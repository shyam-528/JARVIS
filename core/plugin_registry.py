from __future__ import annotations

import base64
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from utils.json_utils import load_json_store, save_json_atomic

from .skill_manifest import MANIFEST_FILENAME, SkillManifest, validate_manifest

# Try to import cryptography for ed25519 signatures
try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric import ed25519

    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False


@dataclass
class InstalledSkill:
    manifest: SkillManifest
    install_path: Path
    installed_at: str
    source: str  # "local", "zip", "git", "registry"
    enabled: bool = True


class PluginRegistry:
    DEFAULT_REGISTRY_URL = (
        "https://raw.githubusercontent.com/jarvis-skills/registry/main/index.json"
    )

    def __init__(self, config, plugins_dir: Path = None):
        self.config = config
        # Default to the repo's plugins/ directory (next to core/)
        if plugins_dir is None:
            plugins_dir = Path(__file__).resolve().parent.parent / "plugins"
        self.plugins_dir = Path(plugins_dir)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.plugins_dir / "index.json"
        self.installed: dict[str, InstalledSkill] = {}

        # Security config
        self.security_enabled = getattr(config, "plugin_security", None)
        if self.security_enabled:
            self.require_signature = self.security_enabled.require_signature
            self.allowed_keys = self.security_enabled.allowed_keys
            self.hash_pinning = self.security_enabled.hash_pinning
            self.allowed_hashes = set(self.security_enabled.allowed_hashes)
        else:
            self.require_signature = False
            self.allowed_keys = []
            self.hash_pinning = False
            self.allowed_hashes = set()

        self._load_index()

    def _load_index(self):
        # P0: shared corrupt-file contract — a corrupt install index
        # quarantines aside (loud) instead of silently forgetting installs.
        data = load_json_store(self.index_path, default={}, owner="plugins", expect=dict)
        for skill_id, entry in data.items():
            try:
                manifest = SkillManifest(**entry["manifest"])
                self.installed[skill_id] = InstalledSkill(
                    manifest=manifest,
                    install_path=Path(entry["install_path"]),
                    installed_at=entry["installed_at"],
                    source=entry["source"],
                    enabled=bool(entry.get("enabled", True)),
                )
            except Exception as exc:
                print(f"[plugins] skipping malformed index entry {skill_id!r}: {exc}")

    def _save_index(self):
        data = {}
        for skill_id, skill in self.installed.items():
            data[skill_id] = {
                "manifest": skill.manifest.__dict__,
                "install_path": str(skill.install_path),
                "installed_at": skill.installed_at,
                "source": skill.source,
                "enabled": skill.enabled,
            }
        # P0: atomic write; _load_index quarantines on corrupt read, so a
        # half-written index can never silently drop the install list.
        save_json_atomic(self.index_path, data)

    def _verify_hash(self, path: Path) -> tuple[bool, str]:
        """Verify file/directory hash against allowed hashes."""
        if not self.hash_pinning:
            return True, ""

        if path.is_file():
            hashes = {self._file_hash(path)}
        else:
            hashes = set()
            for root, _, files in os.walk(path):
                for f in files:
                    fp = Path(root) / f
                    hashes.add(self._file_hash(fp))

        if not self.allowed_hashes:
            return True, ""  # No hashes pinned yet

        for h in hashes:
            if h in self.allowed_hashes:
                return True, h

        return False, list(hashes)[0]

    def _file_hash(self, path: Path) -> str:
        """Compute SHA256 hash of a file."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def _verify_signature(self, path: Path, signature_path: Path) -> bool:
        """Verify ed25519 signature of plugin."""
        if not self.require_signature or not CRYPTO_AVAILABLE:
            return True  # Skip if not required or crypto unavailable

        if not self.allowed_keys:
            return False  # No allowed keys configured

        try:
            # Read signature
            with open(signature_path, "rb") as f:
                signature = f.read()

            # Read data to verify
            if path.is_file():
                with open(path, "rb") as f:
                    data = f.read()
            else:
                # Hash all files in directory
                data_parts = []
                for root, _, files in os.walk(path):
                    for f in sorted(files):
                        fp = Path(root) / f
                        with open(fp, "rb") as f:
                            data_parts.append(f.read())
                data = b"".join(data_parts)

            # Verify against all allowed public keys
            for key_b64 in self.allowed_keys:
                try:
                    public_key = ed25519.Ed25519PublicKey.from_public_bytes(
                        bytes.fromhex(key_b64) if len(key_b64) == 64 else base64.b64decode(key_b64)
                    )
                    public_key.verify(signature, data)
                    return True
                except (InvalidSignature, ValueError):
                    continue

            return False
        except Exception:
            return False

    def _verify_plugin(self, path: Path, skill_id: str) -> tuple[bool, str]:
        """Verify plugin integrity (hash + signature)."""
        # Check hash
        hash_ok, actual_hash = self._verify_hash(path)
        if not hash_ok:
            return False, f"Hash mismatch for {skill_id}: {actual_hash} not in allowed hashes"

        # Check signature
        sig_path = path / "plugin.sig"
        if sig_path.exists():
            if not self._verify_signature(path, sig_path):
                return False, f"Signature verification failed for {skill_id}"
        elif self.require_signature and self.allowed_keys:
            return False, f"Missing signature for {skill_id} (required)"

        return True, "OK"

    def list_installed(self) -> list[InstalledSkill]:
        return list(self.installed.values())

    def get_skill(self, skill_id: str) -> InstalledSkill | None:
        return self.installed.get(skill_id)

    def _install_from_path(self, source_path: Path, skill_id: str) -> InstalledSkill:
        manifest_path = source_path / MANIFEST_FILENAME
        if not manifest_path.exists():
            raise ValueError(f"No {MANIFEST_FILENAME} found in {source_path}")

        manifest = SkillManifest.from_file(manifest_path)
        valid, errors = validate_manifest(manifest.__dict__)
        if not valid:
            raise ValueError(f"Invalid manifest: {errors}")

        target_path = self.plugins_dir / skill_id
        if target_path.exists():
            shutil.rmtree(target_path)
        shutil.copytree(str(source_path), str(target_path))

        # Security verification
        if self.security_enabled:
            ok, msg = self._verify_plugin(target_path, skill_id)
            if not ok:
                shutil.rmtree(target_path, ignore_errors=True)
                raise ValueError(f"Security verification failed: {msg}")

        skill = InstalledSkill(
            manifest=manifest,
            install_path=target_path,
            installed_at=datetime.now().isoformat(),
            source="local",
        )
        self.installed[skill_id] = skill
        self._save_index()
        return skill

    def install_local(self, path: str, skill_id: str = None) -> InstalledSkill:
        source = Path(path).resolve()
        if not source.exists():
            raise ValueError(f"Path not found: {path}")
        if skill_id is None:
            skill_id = source.name
        return self._install_from_path(source, skill_id)

    def install_zip(self, zip_path: str, skill_id: str = None) -> InstalledSkill:
        zip_file = Path(zip_path)
        if not zip_file.exists():
            raise ValueError(f"Zip file not found: {zip_path}")

        with tempfile.TemporaryDirectory() as tmpdir:
            with zipfile.ZipFile(zip_file, "r") as zf:
                zf.extractall(tmpdir)
            extracted = Path(tmpdir)
            skill_dirs = list(extracted.rglob("skill.json"))
            if not skill_dirs:
                raise ValueError("No skill.json found in zip")
            source = skill_dirs[0].parent
            if skill_id is None:
                skill_id = source.name
            return self._install_from_path(source, skill_id)

    def install_from_git(
        self, repo_url: str, skill_id: str = None, branch: str = "main"
    ) -> InstalledSkill:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir) / "repo"
            try:
                subprocess.run(
                    ["git", "clone", "--depth", "1", "--branch", branch, repo_url, str(repo_path)],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
            except subprocess.CalledProcessError as e:
                raise ValueError(f"Git clone failed: {e.stderr.decode()}") from e
            except subprocess.TimeoutExpired:
                raise ValueError("Git clone timed out") from None

            skill_dirs = list(repo_path.rglob("skill.json"))
            if not skill_dirs:
                raise ValueError("No skill.json found in repository")
            source = skill_dirs[0].parent
            if skill_id is None:
                skill_id = source.name
            return self._install_from_path(source, skill_id)

    def install_from_registry(
        self, skill_name: str, version: str = "latest", registry_url: str = None
    ) -> InstalledSkill:
        registry_url = registry_url or self.DEFAULT_REGISTRY_URL
        try:
            with urllib.request.urlopen(registry_url, timeout=30) as resp:
                index = json.loads(resp.read().decode())
        except Exception as e:
            raise ValueError(f"Failed to fetch registry: {e}") from e

        if skill_name not in index:
            raise ValueError(f"Skill '{skill_name}' not found in registry")

        entry = index[skill_name]
        versions = entry.get("versions", {})
        if version == "latest":
            version = max(versions.keys(), key=lambda v: tuple(map(int, v.split("."))))
        if version not in versions:
            raise ValueError(f"Version {version} not found for {skill_name}")

        version_info = versions[version]
        download_url = version_info.get("download_url") or entry.get("download_url")
        if not download_url:
            raise ValueError("No download URL in registry entry")

        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / "skill.zip"
            try:
                urllib.request.urlretrieve(download_url, zip_path)
            except Exception as e:
                raise ValueError(f"Failed to download skill: {e}") from e
            return self.install_zip(str(zip_path), skill_id=skill_name)

    def uninstall(self, skill_id: str) -> bool:
        if skill_id not in self.installed:
            return False
        skill = self.installed[skill_id]
        if skill.install_path.exists():
            shutil.rmtree(skill.install_path)
        del self.installed[skill_id]
        self._save_index()
        return True

    def enable(self, skill_id: str, enabled: bool = True) -> bool:
        """Enable or disable an installed skill."""
        skill = self.installed.get(skill_id)
        if not skill:
            return False
        skill.enabled = enabled
        self._save_index()
        return True

    def update(self, skill_id: str) -> InstalledSkill:
        """Reinstall a skill from its original source."""
        skill = self.installed.get(skill_id)
        if not skill:
            raise ValueError(f"Skill {skill_id} not installed")
        # Reinstall from the currently installed copy (refreshes manifest/index)
        return self._install_from_path(skill.install_path, skill_id)

    def load_skill(self, skill_id: str, toolbox) -> bool:
        """Load a skill's Python module and call its register(toolbox) entry point.

        The skill directory must contain skill.py (or entry_point module).
        Manifest 'entry_point' may be "register" (default, looks for skill.py)
        or a module name like "skill".
        """
        skill = self.installed.get(skill_id)
        if not skill:
            return False
        if not skill.enabled:
            return False
        skill_path = skill.install_path
        if not skill_path.exists():
            return False

        # Resolve the module to import
        ep = (skill.manifest.entry_point or "").strip()
        candidates = []
        if ep and ep != "register" and not ep.endswith(".py"):
            candidates.append(ep)
        for mod in ("skill", "main", "plugin", skill_path.name):
            candidates.append(mod)

        if str(skill_path) not in sys.path:
            sys.path.insert(0, str(skill_path))

        last_err = None
        for mod_name in candidates:
            try:
                module = importlib.import_module(mod_name)
                if hasattr(module, "register"):
                    module.register(toolbox)
                    return True
                last_err = f"module '{mod_name}' has no register() function"
            except ImportError as e:
                last_err = str(e)
                continue
        print(f"Failed to load skill {skill_id}: {last_err}")
        return False

    def load_all_skills(self, toolbox) -> int:
        count = 0
        for skill_id in self.installed:
            if self.load_skill(skill_id, toolbox):
                count += 1
        return count

    def search_registry(self, query: str = "", registry_url: str = None) -> list[dict]:
        registry_url = registry_url or self.DEFAULT_REGISTRY_URL
        try:
            with urllib.request.urlopen(registry_url, timeout=30) as resp:
                index = json.loads(resp.read().decode())
        except Exception:
            return []
        results = []
        for name, entry in index.items():
            if (
                not query
                or query.lower() in name.lower()
                or query.lower() in entry.get("description", "").lower()
            ):
                results.append(
                    {
                        "name": name,
                        "description": entry.get("description", ""),
                        "latest_version": max(entry.get("versions", {}).keys(), default=""),
                        "author": entry.get("author", ""),
                    }
                )
        return results
