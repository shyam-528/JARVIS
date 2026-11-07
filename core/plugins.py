from __future__ import annotations

import contextlib
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .tools import ToolBox


class PluginManager:
    def __init__(self, toolbox: ToolBox, plugin_dirs: list[Path] = None):
        self.toolbox = toolbox
        self.plugin_dirs = plugin_dirs or [Path(__file__).parent.parent / "plugins"]
        self.loaded: dict[str, dict] = {}
        self._hooks: dict[str, list[Callable]] = {}

    def add_plugin_dir(self, path: Path):
        if path not in self.plugin_dirs:
            self.plugin_dirs.append(path)

    def register_hook(self, name: str, callback: Callable):
        self._hooks.setdefault(name, []).append(callback)

    def trigger_hook(self, name: str, *args, **kwargs):
        for cb in self._hooks.get(name, []):
            try:
                cb(*args, **kwargs)
            except Exception as exc:
                print(f"[plugin hook {name}] error: {exc}")

    def load_all(self) -> list[str]:
        loaded = []
        for plugin_dir in self.plugin_dirs:
            if not plugin_dir.exists():
                continue
            for py_file in plugin_dir.glob("*.py"):
                if py_file.name.startswith("_"):
                    continue
                name = py_file.stem
                try:
                    spec = importlib.util.spec_from_file_location(f"jarvis_plugin_{name}", py_file)
                    if spec and spec.loader:
                        module = importlib.util.module_from_spec(spec)
                        sys.modules[f"jarvis_plugin_{name}"] = module
                        spec.loader.exec_module(module)
                        if hasattr(module, "register"):
                            info = module.register(self.toolbox)
                            self.loaded[name] = {"module": module, "info": info}
                            loaded.append(name)
                            print(f"[plugin] loaded: {name}")
                except Exception as exc:
                    print(f"[plugin] failed to load {name}: {exc}")
        return loaded

    def unload(self, name: str) -> bool:
        if name in self.loaded:
            mod_info = self.loaded.pop(name)
            if hasattr(mod_info["module"], "unregister"):
                with contextlib.suppress(Exception):
                    mod_info["module"].unregister(self.toolbox)
            return True
        return False

    def list_loaded(self) -> dict[str, dict]:
        return {k: v["info"] for k, v in self.loaded.items()}

    def get_plugin(self, name: str) -> Any:
        return self.loaded.get(name, {}).get("module")
