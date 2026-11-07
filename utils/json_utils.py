import copy
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_users_from_json(file_path: str | Path) -> list[dict[str, Any]]:
    """Load the ``users`` list from a JSON file.

    Args:
        file_path: Path to the JSON file. Can be a string or :class:`~pathlib.Path`.

    Returns:
        A list of user dictionaries. If the file does not exist, cannot be parsed,
        or does not contain a ``users`` key, an empty list is returned.
    """
    path = Path(file_path)
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.warning("JSON file %s not found. Returning empty user list.", path)
        return []
    except json.JSONDecodeError as e:
        logger.error("Failed to decode JSON from %s: %s", path, e)
        return []

    users = data.get("users")
    if isinstance(users, list):
        return users
    else:
        logger.warning("'users' key missing or not a list in %s. Returning empty list.", path)
        return []


def load_json_store(
    path: str | Path,
    *,
    default: Any,
    owner: str = "store",
    expect: type | tuple[type, ...] | None = None,
) -> Any:
    """Load a JSON store with the preferences.py corrupt-file contract.

    - Missing file → ``default`` (quiet; first boot is not an error).
    - Unparseable JSON, or parsed data failing the ``expect`` shape check →
      the file is moved aside to ``<name>.corrupt-<unix-ts>`` (nothing is
      deleted) and ``default`` is returned. The quarantine is LOUD (print):
      a silently-emptied store is data loss the user can never diagnose.
    - Unreadable file (permissions/lock) → ``default`` + loud print, the
      file is left alone (it may be fine on next boot).
    - Never raises. Returns a deep copy of ``default`` so callers can mutate
      the result without poisoning the default for the next load.
    """
    p = Path(path)
    if not p.exists():
        return copy.deepcopy(default)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        # Unreadable (permissions/lock), NOT corrupt: leave the file alone,
        # it may be fine on next boot — but the fallback must still be loud.
        print(f"[{owner}] unreadable store {p.name} ({type(exc).__name__}: {exc}) — using defaults")
        return copy.deepcopy(default)
    except Exception as exc:
        print(
            f"[{owner}] corrupt store {p.name} ({type(exc).__name__}: {exc}) — moved aside, starting fresh"
        )
        _quarantine(p, owner)
        return copy.deepcopy(default)
    if expect is not None and not isinstance(data, expect):
        print(
            f"[{owner}] corrupt store {p.name} "
            f"(expected {getattr(expect, '__name__', expect)}, got {type(data).__name__}) — "
            "moved aside, starting fresh"
        )
        _quarantine(p, owner)
        return copy.deepcopy(default)
    return data


def _quarantine(path: Path, owner: str) -> None:
    """Move a corrupt store aside. Best-effort, never raises."""
    try:
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        os.replace(path, backup)
        print(f"[{owner}] quarantined to {backup.name} (nothing deleted)")
    except Exception as exc:
        print(f"[{owner}] could not quarantine {path.name}: {type(exc).__name__}: {exc}")


def save_json_atomic(path: str | Path, data: Any) -> bool:
    """Persist JSON atomically (tmp file + os.replace).

    A crash mid-write can never truncate the store: the previous good file
    stays intact until the replacement is fully written. Returns True on
    success, False (plus a loud print) on failure. Never raises.
    """
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except Exception as exc:
        print(f"[store] failed to save {p.name}: {type(exc).__name__}: {exc}")
        return False
