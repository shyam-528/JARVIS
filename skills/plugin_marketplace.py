"""Plugin marketplace tools: search, install, uninstall, list, and inspect
plugins from the registry.
"""

from __future__ import annotations

from core.plugin_registry import PluginRegistry
from core.tools import Tool, ToolBox


def register_marketplace(box: ToolBox, registry: PluginRegistry):
    async def plugin_search(query: str = "") -> str:
        results = registry.search_registry(query)
        if not results:
            return "No skills found in registry"
        lines = []
        for r in results[:10]:
            lines.append(
                f"- {r['name']} v{r['latest_version']}: {r['description'][:80]}... (by {r['author']})"
            )
        return "Registry results:\n" + "\n".join(lines)

    async def plugin_install(
        source: str, skill_id: str = "", version: str = "latest", source_type: str = "registry"
    ) -> str:
        try:
            if source_type == "registry":
                registry.install_from_registry(source, version)
            elif source_type == "git":
                registry.install_from_git(source, skill_id=skill_id or None)
            elif source.endswith(".zip"):
                registry.install_zip(source, skill_id=skill_id or None)
            else:
                registry.install_local(source, skill_id=skill_id or None)
            return "Installed successfully"
        except Exception as e:
            return f"Install failed: {e}"

    async def plugin_uninstall(skill_id: str) -> str:
        if registry.uninstall(skill_id):
            return f"Uninstalled {skill_id}"
        return f"Skill {skill_id} not found"

    async def plugin_list() -> str:
        skills = registry.list_installed()
        if not skills:
            return "No skills installed"
        lines = []
        for s in skills:
            lines.append(
                f"- {s.manifest.name} v{s.manifest.version} ({s.installed_at[:10]}) - {s.manifest.description[:60]}"
            )
        return "Installed skills:\n" + "\n".join(lines)

    async def plugin_info(skill_id: str) -> str:
        skill = registry.get_skill(skill_id)
        if not skill:
            return f"Skill {skill_id} not found"
        m = skill.manifest
        return f"{m.name} v{m.version}\n{m.description}\nAuthor: {m.author}\nTags: {', '.join(m.tags)}\nInstalled: {skill.installed_at[:19]}\nSource: {skill.source}"

    async def plugin_search_registry(query: str = "") -> str:
        results = registry.search_registry(query)
        if not results:
            return "No results"
        return "Registry search:\n" + "\n".join(
            f"- {r['name']} v{r['latest_version']}: {r['description'][:80]}" for r in results[:10]
        )

    box.register(
        Tool(
            name="plugin_search",
            description="Search the skill registry",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "default": ""}},
                "required": [],
            },
            handler=plugin_search_registry,
        )
    )
    box.register(
        Tool(
            name="plugin_install",
            description="Install a skill from registry, git, zip, or local path",
            parameters={
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "skill_id": {"type": "string", "default": ""},
                    "version": {"type": "string", "default": "latest"},
                    "source_type": {
                        "type": "string",
                        "enum": ["registry", "git", "zip", "local"],
                        "default": "registry",
                    },
                },
                "required": ["source"],
            },
            handler=plugin_install,
        )
    )
    box.register(
        Tool(
            name="plugin_uninstall",
            description="Uninstall a skill by ID",
            parameters={
                "type": "object",
                "properties": {"skill_id": {"type": "string"}},
                "required": ["skill_id"],
            },
            handler=plugin_uninstall,
        )
    )
    box.register(
        Tool(
            name="plugin_list",
            description="List installed skills",
            parameters={"type": "object", "properties": {}},
            handler=plugin_list,
        )
    )
    box.register(
        Tool(
            name="plugin_info",
            description="Show detailed info about an installed skill",
            parameters={
                "type": "object",
                "properties": {"skill_id": {"type": "string"}},
                "required": ["skill_id"],
            },
            handler=plugin_info,
        )
    )
    box.register(
        Tool(
            name="plugin_search_registry",
            description="Search the skill registry",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "default": ""}},
                "required": [],
            },
            handler=plugin_search_registry,
        )
    )
