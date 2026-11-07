"""Slash-command table: data-driven registry for the REPL (consolidation step 3).

Extracted from jarvis.py: the COMMAND_ALIASES / COMMANDS / COMMAND_HELP / HELP
metadata, resolve_alias()/get_help(), every /command handler, and the ordered
SLASH_COMMANDS dispatch table. The REPL in jarvis.py keeps only loop control
(/exit) and alias resolution; everything slash-shaped goes through
dispatch_slash(). Handlers print via this module's own rich Console.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from core.config import key_report

if TYPE_CHECKING:
    from core.orchestrator import Orchestrator

console = Console()

COMMAND_ALIASES = {
    "/c": "/chain",
    "/s": "/stats",
    "/k": "/keys",
    "/t": "/tools",
    "/m": "/memory",
    "/ta": "/tasks",
    "/auto": "/automation",
    "/pro": "/proactive",
    "/b": "/briefing",
    "/v": "/vision",
    "/n": "/notify",
    "/a": "/a2a",
    "/p": "/plugins",
    "/pl": "/plugin",
    "/vs": "/vision",
    "/sc": "/screenshot",
    "/rs": "/readscreen",
    "/an": "/analyze",
    "/vc": "/voice",
    "/r": "/reset",
    "/x": "/exit",
    "/q": "/exit",
    "/h": "/help",
    "/?": "/help",
    "/per": "/persona",
    "/pref": "/preferences",
    "/dia": "/dialogue",
    "/sum": "/summarizer",
    "/ag": "/agents",
    "/res": "/result",
}

COMMANDS = [
    "/chain",
    "/stats",
    "/keys",
    "/tools",
    "/memory",
    "/tasks",
    "/automation",
    "/proactive",
    "/briefing",
    "/vision",
    "/notify",
    "/ml",
    "/a2a",
    "/plugins",
    "/voice",
    "/tts",
    "/reset",
    "/exit",
    "/screenshot",
    "/ocr",
    "/readscreen",
    "/analyze",
    "/plugin",
    "/voice",
    "/tts",
    "/reset",
    "/exit",
    "/help",
    "/persona",
    "/preferences",
    "/dialogue",
    "/summarizer",
]

COMMAND_HELP = {
    "/chain": "Show failover chain status",
    "/c": "Alias for /chain",
    "/stats": "Show token usage statistics",
    "/s": "/stats alias",
    "/keys": "Show API key status",
    "/k": "/keys alias",
    "/tools": "List available tools",
    "/t": "/tools alias",
    "/memory": "Browse long-term memory",
    "/forget": "Delete memories matching a query: /forget <query>",
    "/consolidate": "Memory hygiene now: promote facts, list trivia (no deletes)",
    "/m": "/memory alias",
    "/tasks": "Manage scheduled tasks",
    "/ta": "/tasks alias",
    "/automation": "View automation rules",
    "/auto": "/automation alias",
    "/rule": "Rules: /rule list | add <name> | <when> | <do tool> [args] | on|off|del <id>",
    "/proactive": "View proactive suggestions",
    "/pro": "/proactive alias",
    "/briefing": "Show daily briefing",
    "/b": "/briefing alias",
    "/vision": "List monitors and vision commands",
    "/v": "/vision alias",
    "/vs": "/vision alias",
    "/notify": "Send notification: /notify <msg> [--title] [--priority] [--channel] [--tags] [--template]",
    "/n": "/notify alias",
    "/ml": "ML commands: /ml suggestions [ctx] | stats | train | record <action> <tool> [ms] [success]",
    "/a2a": "Show A2A mesh status",
    "/a": "/a2a alias",
    "/plugins": "List installed plugins",
    "/p": "/plugins alias",
    "/plugin": "Plugin management: /plugin install|uninstall|info|search|list [args]",
    "/pl": "/plugin alias",
    "/sc": "/screenshot alias",
    "/ocr": "Extract text from image: /ocr [file.png] [lang=en]",
    "/readscreen": "OCR + analyze screen: /readscreen [prompt=...]",
    "/rs": "/readscreen alias",
    "/analyze": "Analyze image: /analyze [prompt=...] [file.png]",
    "/an": "/analyze alias",
    "/voice": "Enter push-to-talk voice mode",
    "/vc": "/voice alias",
    "/tts": "Spoken replies on/off: /tts on|off (Kokoro voice reads chat answers)",
    "/doctor": "Voice self-check: engines, mics, Kokoro synth, wake-word",
    "/alerts": "Unprompted voice alerts on/off/status: /alerts on|off|status",
    "/reset": "Clear session memory",
    "/r": "/reset alias",
    "/exit": "Exit JARVIS",
    "/x": "/exit alias",
    "/q": "/quit alias",
    "/help": "Show this help",
    "/h": "/help alias",
    "/?": "/help alias",
    "/persona": "Manage personas: /persona list | <name> | add <name> <description> | remove <name>",
    "/per": "/persona alias",
    "/preferences": "Manage preferences: /preferences list | <category> | clear <category> | show",
    "/pref": "/preferences alias",
    "/dialogue": "Show dialogue state: /dialogue [clear]",
    "/dia": "/dialogue alias",
    "/summarizer": "Manage summarizer: /summarizer status | trigger | config",
    "/sum": "/summarizer alias",
    "/agents": "List specialist agents in the pool",
    "/ag": "/agents alias",
    "/bg": "Run task in background: /bg <prompt> (auto-routes) or /bg <agent> <prompt>",
    "/fg": "Chat with an agent in foreground: /fg <agent> <prompt>",
    "/jobs": "Show background job status",
    "/result": "Show result of a finished job: /result <task_id>",
    "/res": "/result alias",
    "/flow": "Workflow manager: /flow list | run <id> [vars k=v] | show <id> | del <id> | on/off <id> | compose <description>",
    "/runs": "Show workflow run history: /runs [run_id for detail]",
    "/health": "Provider health: latency, success rate, budgets, breaker states",
    "/reload": "Hot-reload config.yaml and rebuild provider chain",
    "/backup": "Archive all JARVIS databases to a single zip",
    "/bgmode": "Toggle auto-background: heavy work goes to agents, chat stays foreground (on|off|status)",
    "/init": "Init repo for dev loop: detect type, write .jarvis.json, index code",
    "/review": "Read-only code review of git diff",
    "/test": "Run project tests: /test [path] [cmd]",
    "/pr": "Create GitHub PR: /pr <title> | <body>",
    "/standup": "24h commits + tree + tasks",
    "/triage": "Inbox triage: /triage [sort] (dry-run default)",
    "/wa": "WhatsApp: /wa triage|chats|read <chat>|send <id>|sendto <number> <text>|contact add|del|list",
}

HELP = """Available commands:
  Core:       /chain /stats /keys /tools /memory /tasks /reset /exit
  Dev:        /init /review /test /pr /standup
  Agents:     /agents /bg <prompt> /fg <agent> <prompt> /jobs /result <id>
  History:    /history /new /resume <id>
  Intelligence: /persona /preferences /dialogue /summarizer
  Automation: /automation /rule /proactive /briefing /tasks
  Vision:     /vision /screenshot /ocr /readscreen /analyze
  Voice:      /voice /tts
  ML:         /ml
  A2A:        /a2a
  Plugins:    /plugins /plugin
  System:     /reset /keys /exit /help

Aliases: /c=/chain /s=/stats /k=/keys /t=/tools /m=/memory
         /ta=/tasks /auto=/automation /pro=/proactive /b=/briefing
         /v=/vision /vs=/vision /sc=/screenshot /rs=/readscreen
         /an=/analyze /vc=/voice /r=/reset /x=/exit /q=/quit /h=/help
         /per=/persona /pref=/preferences /dia=/dialogue /sum=/summarizer
         /ag=/agents /res=/result"""


def resolve_alias(cmd: str) -> str:
    """Resolve command aliases to their canonical form."""
    return COMMAND_ALIASES.get(cmd, cmd)


def get_help() -> str:
    categories = {
        "Core": [
            "/chain",
            "/stats",
            "/keys",
            "/tools",
            "/memory",
            "/tasks",
            "/reset",
            "/exit",
        ],
        "Intelligence": ["/persona", "/preferences", "/dialogue", "/summarizer"],
        "Automation": ["/automation", "/rule", "/proactive", "/briefing", "/tasks"],
        "Vision": ["/vision", "/screenshot", "/ocr", "/readscreen", "/analyze"],
        "Voice": ["/voice", "/tts", "/vc"],
        "ML": ["/ml"],
        "/a2a": ["/a2a", "/a"],
        "Plugins": ["/plugins", "/plugin"],
        "/voice": ["/voice", "/vc"],
        "/tts": ["/tts"],
        "System": ["/reset", "/keys", "/exit", "/help"],
    }
    lines = ["Available commands:"]
    for cat, cmds in categories.items():
        lines.append(f"\n  [{cat}]")
        for cmd in cmds:
            if cmd in COMMAND_HELP:
                desc = COMMAND_HELP[cmd]
                lines.append(f"  {cmd:<15} {desc}")
    lines.append("\n  Aliases: /c=/chain, /s=/stats, /k=/keys, /t=/tools, /m=/memory")
    lines.append("  /ta=/tasks, /auto=/automation, /pro=/proactive, /b=/briefing")
    lines.append("  /v=/vision, /vs=/vision, /sc=/screenshot, /rs=/readscreen")
    lines.append("  /an=/analyze, /vc=/voice, /r=/reset, /x=/exit, /q=/quit, /h=/help")
    return "\n".join(lines)


def _print_memory(orch: Orchestrator) -> None:
    rows = orch.memory.all(30)
    if not rows:
        console.print("[dim]long-term memory is empty — tell Jarvis facts to remember[/]")
        return
    table = Table(
        title=f"Long-term memory ({orch.memory.count()} facts)",
        header_style="bold cyan",
    )
    table.add_column("id", justify="right")
    table.add_column("when")
    table.add_column("fact", overflow="fold")
    for mid, ts, text in rows:
        table.add_row(str(mid), ts, text)
    console.print(table)


def print_chain(orch: Orchestrator) -> None:
    table = Table(title="Failover chain", header_style="bold cyan")
    table.add_column("#", justify="right")
    table.add_column("brain")
    table.add_column("model")
    table.add_column("state")
    table.add_column("budget left", justify="right")
    for priority, name, model, state, left in orch.chain_status():
        style = (
            "green"
            if state == "READY"
            else (
                "red"
                if state in ("BUDGET-OUT",) or state.startswith("COOLING")
                else "dim"
                if state == "NO-CHAT"
                else "yellow"
            )
        )
        table.add_row(str(priority), name, model, f"[{style}]{state}[/]", left)
    console.print(table)


def _print_stats(orch: Orchestrator) -> None:
    s = orch.stats()
    table = Table(title="Today's usage", header_style="bold cyan")
    table.add_column("brain")
    table.add_column("~tokens", justify="right")
    for name, tokens in s["usage"] or [("(nothing yet)", 0)]:
        table.add_row(name, f"{tokens:,}")
    console.print(table)
    console.print(
        f"total failover hops: [bold]{s['total_hops']}[/]   last brain: [bold]{s['last_brain'] or '—'}[/]"
    )
    console.print(f"context: {s['context_messages']} msgs (budget ~{s['context_budget']:,} tok)")


def print_keys() -> None:
    r = key_report()
    if r.active:
        console.print("[green]active:[/] " + ", ".join(r.active))
    if r.missing_env:
        console.print("[yellow]missing keys:[/] " + ", ".join(r.missing_env))
        console.print("put them in [bold].env[/] — see [bold].env.example[/]")
    if r.disabled:
        console.print("[dim]disabled:[/] " + ", ".join(r.disabled))


def _print_history(orch: Orchestrator) -> None:
    sessions = orch.persistence.list_sessions(20)
    if not sessions:
        console.print("[dim]no conversation history[/]")
        return
    table = Table(
        title=f"Conversation History ({orch.persistence.total_sessions()} sessions)",
        header_style="bold cyan",
    )
    table.add_column("ID")
    table.add_column("Title", overflow="fold")
    table.add_column("Messages", justify="right")
    table.add_column("Last Active")
    for s in sessions:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["updated_at"]))
        table.add_row(s["id"], s["title"], str(s["message_count"]), ts)
    console.print(table)
    console.print("[dim]Use /resume <id> to continue a conversation, /new for fresh start[/]")


def _print_pending(orch: Orchestrator) -> None:
    pending = orch.code_agent.queue.get_pending()
    if not pending:
        console.print("[dim]no pending changes[/]")
        return
    for c in pending:
        console.print(f"\n[bold yellow]┌─ {c.change_id} ─ {c.action} ─ {c.file_path}[/]")
        if c.diff:
            console.print(c.diff[:1500])
        elif c.command:
            console.print(f"  Command: {c.command}")
    console.print(
        f"\n[dim]{len(pending)} pending. Use /approve or /approve_all, /reject to dismiss.[/]"
    )


def _do_approve(orch: Orchestrator) -> None:
    pending = orch.code_agent.queue.get_pending()
    if not pending:
        console.print("[dim]no pending changes[/]")
        return
    if len(pending) == 1:
        result = orch.code_agent.approve(pending[0].change_id)
        if "error" in result:
            console.print(f"[red]{result['error']}[/]")
        else:
            console.print(
                f"[green]✔ Applied: {result.get('file_path', result.get('change_id'))}[/]"
            )
            if result.get("output"):
                console.print(str(result["output"])[:1000])
    else:
        console.print(f"[yellow]{len(pending)} pending changes. Use /approve_all or specify.[/]")


def _do_approve_all(orch: Orchestrator) -> None:
    results = orch.code_agent.approve_all()
    for r in results:
        status = "✔" if r.get("applied") else "✖"
        console.print(f"[green]{status} {r.get('file_path', r.get('change_id'))}[/]")


def _do_reject(orch: Orchestrator) -> None:
    pending = orch.code_agent.queue.get_pending()
    if not pending:
        console.print("[dim]no pending changes[/]")
        return
    for c in pending:
        orch.code_agent.reject(c.change_id)
        console.print(f"[red]✖ Rejected: {c.description}[/]")


def _print_tasks(orch: Orchestrator) -> None:
    tasks = orch.scheduler.list_tasks()
    if not tasks:
        console.print("[dim]no scheduled tasks[/]")
        return
    table = Table(title="Scheduled tasks", header_style="bold cyan")
    table.add_column("id", justify="right")
    table.add_column("name")
    table.add_column("trigger")
    table.add_column("action")
    table.add_column("next run")
    table.add_column("runs", justify="right")
    for t in tasks:
        table.add_row(
            t.id,
            t.name,
            f"{t.trigger_type}:{t.trigger_spec}",
            t.action,
            t.next_run or "never",
            str(t.run_count),
        )
    console.print(table)


def _get_automation(orch: Orchestrator):
    ae = getattr(orch, "_automation", None)
    if ae is None:
        from core.automation import AutomationEngine

        ae = AutomationEngine(orch.cfg, orch)
        orch._automation = ae
    return ae


def _do_rule(orch: Orchestrator, cmd: str) -> None:
    """Manage automation rules.
    /rule list
    /rule add <name> | <when> | <do tool> [json-args]
        when: text = match user messages (regex) | tool:<name> = after that tool runs
        ex: /rule add deploy-guard | deploy | do notify {"message":"deploy mentioned"}
    /rule on|off|del <id>
    """
    import json as _j

    rest = cmd[5:].strip()
    if not rest or rest == "list":
        _print_automation(orch)
        return
    ae = _get_automation(orch)
    sub, _, arg = rest.partition(" ")
    sub = sub.lower()
    if sub in ("on", "off", "del", "delete", "remove"):
        rid = arg.strip()
        if not rid:
            console.print("[yellow]Usage: /rule on|off|del <id>[/]")
            return
        if sub in ("del", "delete", "remove"):
            ok = ae.remove_rule(rid)
            console.print(f"[green]deleted {rid}[/]" if ok else f"[red]no rule {rid}[/]")
        else:
            ok = ae.enable_rule(rid, sub == "on")
            console.print(
                f"[green]{rid} {'enabled' if sub == 'on' else 'disabled'}[/]"
                if ok
                else f"[red]no rule {rid}[/]"
            )
        return
    if sub == "add":
        parts = [p.strip() for p in arg.split("|")]
        if len(parts) < 3:
            console.print("[yellow]Usage: /rule add <name> | <when> | <do tool> [json-args][/]")
            console.print("[dim]when: text (regex on your messages) or tool:<name>[/]")
            return
        name, when, do = parts[0], parts[1], parts[2]
        if not name or not when or not do:
            console.print("[yellow]name, when and do-tool are all required[/]")
            return
        do = do.strip()
        if do.lower().startswith("do "):
            do = do[3:].strip()
        elif do.lower() == "do":
            console.print("[yellow]do which tool? e.g. | do notify {...}[/]")
            return
        if when.lower().startswith("tool:"):
            trigger = {"type": "tool_result", "tool": when[5:].strip()}
        else:
            trigger = {"type": "command", "pattern": when}
        tokens = do.split(None, 1)
        tool, args_s = tokens[0], (tokens[1] if len(tokens) > 1 else "{}")
        if tool not in orch.toolbox.names():
            console.print(f"[red]unknown tool '{tool}' — see /tools[/]")
            return
        try:
            args = _j.loads(args_s) if args_s.strip().startswith("{") else {}
            if args_s.strip() and not args_s.strip().startswith("{"):
                console.print("[yellow]args must be JSON object or omitted[/]")
                return
        except Exception as exc:
            console.print(f"[red]bad args JSON: {exc}[/]")
            return
        try:
            rid = ae.add_rule(name, trigger, [{"tool": tool, "args": args}])
            console.print(f"[green]rule '{name}' added as {rid}[/]")
        except ValueError as exc:
            console.print(f"[red]{exc}[/]")
        return
    console.print(
        "[yellow]Usage: /rule list | add <name> | <when> | <do tool> [args] | on|off|del <id>[/]"
    )


def _print_automation(orch: Orchestrator) -> None:
    try:
        # Reuse the live engine (run counts) instead of a throwaway instance.
        ae = _get_automation(orch)
        rules = ae.list_rules_formatted()
        if not rules:
            console.print("[dim]no automation rules[/]")
            return
        table = Table(title="Automation rules", header_style="bold cyan")
        table.add_column("id")
        table.add_column("name")
        table.add_column("trigger")
        table.add_column("actions")
        table.add_column("runs", justify="right")
        for r in rules:
            table.add_row(r["id"], r["name"], str(r["trigger"]), str(r["actions"]), str(r["runs"]))
        console.print(table)
    except Exception as exc:
        console.print(f"[red]automation error:[/] {exc}")


def _print_proactive(orch: Orchestrator) -> None:
    suggestions = orch.proactive.get_active(10)
    if not suggestions:
        console.print("[dim]no proactive suggestions[/]")
        return
    table = Table(title="Proactive suggestions", header_style="bold cyan")
    table.add_column("id")
    table.add_column("type")
    table.add_column("title")
    table.add_column("confidence", justify="right")
    for s in suggestions:
        table.add_row(s.id, s.type, s.title, f"{s.confidence:.0%}")
    console.print(table)


def _print_plugins(orch: Orchestrator) -> None:
    skills = orch.registry.list_installed()
    if not skills:
        console.print("[dim]no skills installed — use /plugin install to add[/]")
        return
    table = Table(title="Installed Skills", header_style="bold cyan")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Description")
    table.add_column("Source")
    for s in skills:
        table.add_row(s.manifest.name, s.manifest.version, s.manifest.description[:60], s.source)
    console.print(table)
    console.print("[dim]Use /plugin install <source> to add skills[/]")


def _print_a2a(orch: Orchestrator) -> None:
    try:
        mesh = orch.mesh
        if not mesh.running:
            console.print("[dim]A2A mesh not running[/]")
            return

        table = Table(title="A2A Mesh Network", header_style="bold cyan")
        table.add_column("Agent ID")
        table.add_column("Name")
        table.add_column("Host:Port")
        table.add_column("Capabilities")
        for peer_id, peer in mesh.peers.items():
            caps = ", ".join(peer.get("capabilities", {}).get("skills", []))
            table.add_row(
                peer_id,
                peer.get("name", "Unknown"),
                f"{peer.get('host', '?')}:{peer.get('port', 0)}",
                caps[:50] + ("..." if len(caps) > 50 else ""),
            )
        console.print(table)
        console.print(f"[dim]Local agent: {mesh.local_agent_id} on {mesh.host}:{mesh.port}[/]")
    except Exception as exc:
        console.print(f"[red]A2A error:[/] {exc}")
    skills = orch.registry.list_installed()
    if not skills:
        console.print("[dim]no skills installed — use /plugin install to add[/]")
        return
    table = Table(title="Installed Skills", header_style="bold cyan")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Description")
    table.add_column("Source")
    for s in skills:
        table.add_row(s.manifest.name, s.manifest.version, s.manifest.description[:60], s.source)
    console.print(table)
    console.print("[dim]Use /plugin install <source> to add skills[/]")


async def _do_plugin(orch: Orchestrator, cmd: str) -> None:
    parts = cmd.split(maxsplit=3)
    if len(parts) < 2:
        console.print("[yellow]Usage: /plugin <install|uninstall|info|search|list> [args][/]")
        return
    sub = parts[1].lower()
    args = parts[2] if len(parts) > 2 else ""
    try:
        if sub == "install":
            parts = args.split(maxsplit=3)
            if not parts:
                console.print(
                    "[yellow]Usage: /plugin install <local|git|registry> <source> [id=...] [version=...][/]"
                )
                return
            source_type = parts[0].lower()
            source = parts[1] if len(parts) > 1 else ""
            skill_id = ""
            version = "latest"
            for p in parts[2:]:
                if p.startswith("id="):
                    skill_id = p.split("=", 1)[1]
                elif p.startswith("version="):
                    version = p.split("=", 1)[1]
            if source_type == "local":
                orch.registry.install_local(source, skill_id=skill_id or None)
                console.print("[green]Installed locally[/]")
            elif source_type == "git":
                orch.registry.install_from_git(source, skill_id=skill_id or None, branch=version)
                console.print("[green]Installed from git[/]")
            elif source_type == "registry":
                orch.registry.install_from_registry(source, version)
                console.print("[green]Installed from registry[/]")
            else:
                console.print(
                    "[yellow]Usage: /plugin install <local|git|registry> <source> [id=...] [version=...][/]"
                )
        elif sub == "uninstall":
            skill_id = args.strip()
            if orch.registry.uninstall(skill_id):
                console.print(f"[green]Uninstalled {skill_id}[/]")
            else:
                console.print(f"[red]Skill {skill_id} not found[/]")
        elif sub == "info":
            skill = orch.registry.get_skill(args.strip())
            if skill:
                m = skill.manifest
                console.print(
                    f"{m.name} v{m.version}\n{m.description}\nAuthor: {m.author}\nSource: {skill.source}"
                )
            else:
                console.print("[red]Skill not found[/]")
        elif sub == "search":
            results = orch.registry.search_registry(args.strip())
            if not results:
                console.print("[dim]No results[/]")
            else:
                for r in results[:10]:
                    console.print(f"- {r['name']} v{r['latest_version']}: {r['description'][:80]}")
        elif sub == "list":
            skills = orch.registry.list_installed()
            if not skills:
                console.print("[dim]no skills installed[/]")
            else:
                for s in skills:
                    console.print(f"- {s.manifest.name} v{s.manifest.version} ({s.source})")
        else:
            console.print("[yellow]Usage: /plugin <install|uninstall|info|search|list> [args][/]")
    except Exception as e:
        console.print(f"[red]Plugin error:[/] {e}")


async def _do_briefing(orch: Orchestrator) -> None:
    from skills.briefing import daily_briefing

    briefing = await daily_briefing()
    console.print("[bold cyan]Briefing:[/]")
    console.print(briefing, markup=False, highlight=False)


def _print_audio() -> None:
    try:
        from voice.audio import list_devices

        devices = list_devices()
        table = Table(title="Audio devices", header_style="bold cyan")
        table.add_column("#", justify="right")
        table.add_column("Name", overflow="fold")
        table.add_column("Dir")
        table.add_column("Default")
        for d in devices:
            direction = "in" if d["input"] else "out"
            default = "★" if (d["default_in"] or d["default_out"]) else ""
            table.add_row(str(d["index"]), d["name"], direction, default)
        console.print(table)
        console.print(
            "[dim]Voice 3.0: kokoro neural local · webrtcvad · barge-in ('stop') · 'repeat'[/]"
        )
    except Exception as exc:
        console.print(f"[red]audio error:[/] {exc}")


def _print_vision(orch: Orchestrator) -> None:
    try:
        from core.vision import ScreenCaptureManager

        mgr = ScreenCaptureManager()
        monitors = mgr.list_monitors()
        mgr.close()
        if not monitors:
            console.print("[dim]no monitors detected[/]")
            return
        table = Table(title="Monitors", header_style="bold cyan")
        table.add_column("#")
        table.add_column("Resolution")
        table.add_column("Position")
        for i, m in enumerate(monitors, 1):
            table.add_row(str(i), f"{m['width']}x{m['height']}", f"({m['left']},{m['top']})")
        console.print(table)
        console.print("[dim]Use /screenshot, /ocr, /readscreen, /analyze commands[/]")
    except Exception as exc:
        console.print(f"[red]vision error:[/] {exc}")


async def _do_notify(orch: Orchestrator, cmd: str) -> None:
    parts = cmd.split(maxsplit=2)
    if len(parts) < 2:
        console.print(
            "[yellow]Usage: /notify <message> [--title TITLE] [--priority min|low|default|high|max] [--channel ntfy|pushover|webhook] [--tags tag1,tag2] [--template NAME] [key=value...][/]"
        )
        return
    message = parts[1]
    args = parts[2] if len(parts) > 2 else ""

    import shlex

    try:
        arg_parts = shlex.split(args)
    except ValueError:
        console.print("[red]Invalid arguments[/]")
        return

    title = ""
    priority = "default"
    channel = "ntfy"
    tags = []
    template = ""
    template_vars = {}

    i = 0
    while i < len(arg_parts):
        arg = arg_parts[i]
        if arg in ("--title", "-t"):
            title = arg_parts[i + 1] if i + 1 < len(arg_parts) else ""
            i += 2
        elif arg in ("--priority", "-p"):
            priority = arg_parts[i + 1] if i + 1 < len(arg_parts) else "default"
            i += 2
        elif arg in ("--channel", "-c"):
            channel = arg_parts[i + 1] if i + 1 < len(arg_parts) else "ntfy"
            i += 2
        elif arg in ("--tags",):
            tags = (
                [t.strip() for t in arg_parts[i + 1].split(",")] if i + 1 < len(arg_parts) else []
            )
            i += 2
        elif arg in ("--template",):
            template = arg_parts[i + 1] if i + 1 < len(arg_parts) else ""
            i += 2
        elif "=" in arg and not arg.startswith("--"):
            key, value = arg.split("=", 1)
            template_vars[key] = value
            i += 1
        else:
            i += 1

    from core.notify import Channel, Priority

    try:
        pri = Priority[priority.upper()]
    except KeyError:
        pri = Priority.DEFAULT
    try:
        ch = Channel[channel.upper()]
    except KeyError:
        ch = Channel.NTFY
    tags_list = [t.strip() for t in tags] if tags else []

    try:
        result = await orch.notify_manager.notify(
            message=message,
            title=title,
            priority=pri,
            tags=tags_list,
            channel=ch,
            template=template,
            **template_vars,
        )
        if result.get("success"):
            console.print(f"[green]Notification sent via {channel}[/]")
        else:
            console.print(f"[red]Failed:[/] {result.get('error', 'Unknown error')}")
    except Exception as e:
        console.print(f"[red]Notify error:[/] {e}")


def _do_screenshot(orch: Orchestrator, cmd: str) -> None:
    parts = cmd.split()
    monitor = 1
    save = True
    for part in parts[1:]:
        if part.startswith("m="):
            monitor = int(part.split("=")[1])
        elif part == "nosave":
            save = False
    from core.vision import screenshot

    result = screenshot(monitor=monitor, save=save)
    if result["success"]:
        console.print(
            f"[green]Screenshot saved:[/] {result['path']} ({result['size'][0]}x{result['size'][1]})"
        )
    else:
        console.print(f"[red]Error:[/] {result['error']}")


def _do_ocr(orch: Orchestrator, cmd: str) -> None:
    parts = cmd.split()
    path = ""
    lang = "eng"
    for part in parts[1:]:
        if part.startswith("lang="):
            lang = part.split("=")[1]
        elif not path and not part.startswith("-"):
            path = part
    from PIL import Image

    from core.vision import ocr_image

    if path:
        text = ocr_image(image_path=path, lang=lang)
    else:
        from PIL import ImageGrab

        img = ImageGrab.grabclipboard()
        if isinstance(img, Image.Image):
            text = ocr_image(image=img, lang=lang)
        else:
            console.print("[red]No image path provided and clipboard empty[/]")
            return
    console.print(f"[bold]OCR result:[/]\n{text}")


async def _do_read_screen(orch: Orchestrator, cmd: str) -> None:
    parts = cmd.split()
    prompt = "What text is visible on screen?"
    for part in parts[1:]:
        if part.startswith("prompt="):
            prompt = part.split("=", 1)[1]
    from core.vision import ImageAnalyzer, OCREngine, ScreenCaptureManager

    cap = ScreenCaptureManager().capture_full_screen()
    ocr_engine = OCREngine()
    text = ocr_engine.extract_text(cap.image)
    analyzer = ImageAnalyzer(orch)
    analysis = await analyzer.analyze_with_model(cap.image, f"{prompt}\n\nOCR text found:\n{text}")
    console.print(f"[bold]OCR text:[/]\n{text}\n\n[bold]Analysis:[/]\n{analysis}")


async def _do_analyze(orch: Orchestrator, cmd: str) -> None:
    parts = cmd.split()
    prompt = "Describe this image"
    path = ""
    for part in parts[1:]:
        if part.startswith("prompt="):
            prompt = part.split("=", 1)[1]
        elif not path and not part.startswith("-"):
            path = part
    from PIL import Image

    from core.vision import ImageAnalyzer

    analyzer = ImageAnalyzer(orch)
    if path:
        img = Image.open(path)
    else:
        from core.vision import ScreenCaptureManager

        cap = ScreenCaptureManager().capture_full_screen()
        img = cap.image
    analysis = await analyzer.analyze_with_model(img, prompt)
    console.print(f"[bold]Analysis:[/]\n{analysis}")


async def _do_ml(orch: Orchestrator, cmd: str) -> None:
    parts = cmd.split(maxsplit=2)
    if len(parts) < 2:
        console.print("[yellow]Usage: /ml <suggestions|stats|train|record> [args][/]")
        return
    sub = parts[1].lower()
    args = parts[2] if len(parts) > 2 else ""
    try:
        if sub == "suggestions":
            context = {}
            if args:
                import json

                with contextlib.suppress(json.JSONDecodeError):
                    context = json.loads(args)
            suggestions = orch.ml_proactive.get_suggestions(context)
            if not suggestions:
                console.print("[dim]No ML suggestions at this time[/]")
                return
            console.print("[bold cyan]ML Proactive Suggestions:[/]")
            for s in suggestions:
                console.print(
                    f"  [{s.type.value.upper()}] {s.title}: {s.description} (confidence: {s.confidence:.0%})"
                )
        elif sub == "stats":
            stats = orch.ml_proactive.get_stats()
            console.print("[bold cyan]ML Engine Stats:[/]")
            for k, v in stats.items():
                console.print(f"  {k}: {v}")
        elif sub == "train":
            console.print("[cyan]Retraining ML models...[/]")
            await orch.ml_proactive.behavior_analyzer.train_anomaly_detector()
            await asyncio.to_thread(orch.ml_proactive.prediction_engine.train)
            orch.ml_proactive._save_models()
            console.print("[green]ML models retrained and saved[/]")
        elif sub == "record":
            parts = args.split()
            if len(parts) < 2:
                console.print(
                    "[yellow]Usage: /ml record <action_type> <tool> [duration_ms] [success=true/false][/]"
                )
                return
            action_type = parts[0]
            tool = parts[1]
            duration = int(parts[2]) if len(parts) > 2 else 0
            success = parts[3].lower() != "false" if len(parts) > 3 else True
            orch.ml_proactive.record_action(action_type, tool, duration, success)
            console.print(f"[green]Recorded:[/] {action_type} via {tool}")
        else:
            console.print("[yellow]Usage: /ml <suggestions|stats|train|record> [args][/]")
    except Exception as e:
        console.print(f"[red]ML error:[/] {e}")


def _do_persona(orch: Orchestrator, cmd: str) -> None:
    """Handle /persona command."""
    parts = cmd.split(maxsplit=2)
    if len(parts) < 2:
        console.print(
            "[yellow]Usage: /persona list | <name> | add <name> <description> | remove <name>[/]"
        )
        return

    sub = parts[1].lower()

    if sub == "list":
        table = Table(title="Available Personas", header_style="bold cyan")
        table.add_column("Name")
        table.add_column("Description")
        table.add_column("Honorific")
        table.add_column("Verbosity")
        table.add_column("Active")
        active = orch.persona.active_persona
        for name, profile in orch.persona.list_personas().items():
            table.add_row(
                name,
                profile.description[:60],
                profile.honorific or "—",
                profile.verbosity,
                "★" if name == active else "",
            )
        console.print(table)
    elif sub == "add":
        if len(parts) < 3:
            console.print("[yellow]Usage: /persona add <name> <description>[/]")
            return
        import shlex

        try:
            args = shlex.split(parts[2])
        except ValueError:
            console.print("[red]Invalid arguments[/]")
            return
        if len(args) < 2:
            console.print(
                "[yellow]Usage: /persona add <name> <description> [honorific] [verbosity][/]"
            )
            return
        name = args[0]
        description = args[1]
        honorific = args[2] if len(args) > 2 else ""
        verbosity = args[3] if len(args) > 3 else "concise"
        from core.persona import PersonaProfile

        profile = PersonaProfile(
            name=name,
            description=description,
            honorific=honorific,
            verbosity=verbosity,
        )
        if orch.persona.add_persona(profile):
            console.print(f"[green]Added persona '{name}'[/]")
        else:
            console.print(f"[red]Persona '{name}' already exists[/]")
    elif sub == "remove":
        if len(parts) < 3:
            console.print("[yellow]Usage: /persona remove <name>[/]")
            return
        name = parts[2].strip()
        if orch.persona.remove_persona(name):
            console.print(f"[green]Removed persona '{name}'[/]")
        else:
            console.print(f"[red]Cannot remove persona '{name}' (not found or default)[/]")
    else:
        name = sub
        if orch.persona.set_active(name):
            console.print(f"[green]Switched to persona '{name}'[/]")
            if orch.history and orch.history[0].get("role") == "system":
                base = orch.cfg.system_prompt
                orch.history[0]["content"] = orch.persona.build_system_prompt(base)
        else:
            console.print(
                f"[red]Persona '{name}' not found. Use '/persona list' to see available.[/]"
            )


def _do_preferences(orch: Orchestrator, cmd: str) -> None:
    """Handle /preferences command."""
    parts = cmd.split(maxsplit=2)
    if len(parts) < 2:
        console.print("[yellow]Usage: /preferences list | <category> | clear <category> | show[/]")
        return

    sub = parts[1].lower()

    if sub in ("list", "show"):
        prefs = orch.preferences.list_all()
        if not prefs:
            console.print("[dim]No preferences learned yet[/]")
            return
        table = Table(title="Learned Preferences", header_style="bold cyan")
        table.add_column("Key")
        table.add_column("Value")
        table.add_column("Category")
        table.add_column("Confidence")
        table.add_column("Source")
        table.add_column("Usage")
        for key, pref in prefs.items():
            table.add_row(
                key,
                pref.value[:50],
                pref.category,
                f"{pref.confidence:.0%}",
                pref.source,
                str(pref.usage_count),
            )
        console.print(table)
    elif sub == "clear":
        if len(parts) < 3:
            console.print("[yellow]Usage: /preferences clear <category>[/]")
            return
        category = parts[2].strip()
        count = orch.preferences.clear_category(category)
        console.print(f"[green]Cleared {count} preferences from category '{category}'[/]")
    else:
        prefs = orch.preferences.get_for_category(sub)
        if not prefs:
            console.print(f"[dim]No preferences in category '{sub}'[/]")
            return
        table = Table(title=f"Preferences: {sub}", header_style="bold cyan")
        table.add_column("Key")
        table.add_column("Value")
        table.add_column("Confidence")
        table.add_column("Source")
        for pref in prefs:
            table.add_row(
                pref.key,
                pref.value,
                f"{pref.confidence:.0%}",
                pref.source,
            )
        console.print(table)


def _do_dialogue(orch: Orchestrator, cmd: str) -> None:
    """Handle /dialogue command."""
    parts = cmd.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip().lower() == "clear":
        orch.dialogue = type(orch.dialogue)(
            max_threads=orch.cfg.dialogue.max_threads,
            max_history=orch.cfg.dialogue.max_history,
        )
        console.print("[green]Dialogue state cleared[/]")
        return

    state = orch.dialogue.state
    console.print("[bold cyan]Dialogue State:[/]")
    console.print(f"  Turn count: {state.turn_count}")
    console.print(f"  Current intent: {state.last_user_intent or '—'}")
    console.print(f"  Last assistant action: {state.last_assistant_action or '—'}")
    console.print(f"  Pending questions: {len(state.pending_questions)}")

    if state.current_thread:
        t = state.current_thread
        console.print(f"\n  [bold]Current thread:[/] {t.topic}")
        console.print(f"  Entities: {', '.join(t.entities) if t.entities else '—'}")
        console.print(f"  Started at turn: {t.start_turn}")

    if state.threads:
        console.print(f"\n  [bold]Previous threads ({len(state.threads)}):[/]")
        for i, t in enumerate(state.threads[-3:], 1):
            console.print(f"    {i}. {t.summary}")


def _do_summarizer(orch: Orchestrator, cmd: str) -> None:
    """Handle /summarizer command."""
    parts = cmd.split(maxsplit=1)
    sub = parts[1].strip().lower() if len(parts) > 1 else "status"

    if sub == "status":
        total_chars = sum(len(str(m.get("content", ""))) for m in orch.history)
        estimated_tokens = total_chars // 4
        budget = orch.context_budget_tokens
        should = orch.summarizer.should_summarize(orch.history, orch.context_budget_tokens)

        table = Table(title="Summarizer Status", header_style="bold cyan")
        table.add_column("Metric")
        table.add_column("Value")
        table.add_row("Enabled", str(orch.summarizer.config.enabled))
        table.add_row("Model", orch.summarizer.config.model)
        table.add_row("History messages", str(len(orch.history)))
        table.add_row("Estimated tokens", f"{estimated_tokens:,}")
        table.add_row("Budget", f"{budget:,}")
        table.add_row("Usage ratio", f"{estimated_tokens / budget:.1%}")
        table.add_row("Should summarize", str(should))
        table.add_row("Min turns to summarize", str(orch.summarizer.config.min_turns_to_summarize))
        console.print(table)
    elif sub == "trigger":
        if len(orch.history) <= 6:
            console.print("[yellow]Not enough history to summarize[/]")
            return
        messages_to_summarize = orch.history[1:-5]
        if len(messages_to_summarize) < orch.summarizer.config.min_turns_to_summarize:
            console.print(
                f"[yellow]Need at least {orch.summarizer.config.min_turns_to_summarize} messages to summarize[/]"
            )
            return

        import asyncio

        async def do_summarize():
            summary = await orch.summarizer.create_rolling_summary(orch, messages_to_summarize)
            if summary:
                orch.history = (
                    [orch.history[0]] + [{"role": "system", "content": summary}] + orch.history[-5:]
                )
                console.print("[green]Summarization complete[/]")
                console.print(f"[dim]{summary[:200]}...[/]")
            else:
                console.print("[red]Summarization failed[/]")

        asyncio.run(do_summarize())
    elif sub == "config":
        c = orch.summarizer.config
        table = Table(title="Summarizer Config", header_style="bold cyan")
        table.add_column("Setting")
        table.add_column("Value")
        table.add_row("enabled", str(c.enabled))
        table.add_row("model", c.model)
        table.add_row("max_summary_tokens", str(c.max_summary_tokens))
        table.add_row("trigger_ratio", str(c.trigger_ratio))
        table.add_row("min_turns_to_summarize", str(c.min_turns_to_summarize))
        console.print(table)
    else:
        console.print("[yellow]Usage: /summarizer status | trigger | config[/]")


# ==================== MULTI-AGENT COMMANDS ====================


def _print_agents(orch: Orchestrator) -> None:
    pool = orch.agent_pool
    brain_mgr = getattr(orch, "brain_manager", None)
    table = Table(title="Agent Pool", header_style="bold cyan")
    table.add_column("Agent")
    table.add_column("Role")
    table.add_column("Dedicated brain")
    table.add_column("Status")
    for a in pool.agents_overview():
        brain = "—"
        if brain_mgr is not None:
            ba = brain_mgr.get(a["name"])
            if ba:
                brain = ba.primary
                if ba.fallbacks:
                    brain += f" (+{len(ba.fallbacks)})"
        provs = ", ".join(a["providers"][:2])
        table.add_row(
            f"{a['icon']} {a['name']}",
            a["description"][:45],
            brain if brain != "—" or provs else (provs or brain),
            "[yellow]BUSY[/]" if a["busy"] else "[green]idle[/]",
        )
    console.print(table)
    console.print(
        "[dim]/bg <prompt> auto-routes · /bg <agent> <prompt> explicit · /fg <agent> <prompt> foreground · /jobs · /result <id>[/]"
    )
    console.print(
        "[dim]/agent new <name> <desc> | /agent brain <agent> <provider> | /agent brain auto | /agent del <name>[/]"
    )


async def _do_bg(orch: Orchestrator, cmd: str) -> None:
    """Run a task in the background with the best agent."""
    pool = orch.agent_pool
    rest = cmd[3:].strip() if cmd.lower().startswith("/bg") else ""
    if not rest:
        console.print("[yellow]Usage: /bg <prompt>  |  /bg <agent> <prompt>[/]")
        console.print(f"[dim]agents: {', '.join(pool.agents.keys())}[/]")
        return
    words = rest.split(maxsplit=1)
    if len(words) == 2 and words[0].lower() in pool.agents:
        agent = words[0].lower()
        prompt = words[1].strip()
    else:
        agent = pool.route(rest)
        prompt = rest
    spec = pool.agents[agent]
    task_id = pool.submit(agent, prompt)
    console.print(f"[green]{spec.icon} {agent} agent picked up job [bold]{task_id}[/][/]")
    console.print(f"[dim]prompt: {prompt[:90]}{'…' if len(prompt) > 90 else ''}[/]")
    console.print(
        "[dim]keep chatting — I'll notify you when it's done. /jobs to check · /result "
        + task_id
        + "[/]"
    )


async def _do_fg(orch: Orchestrator, cmd: str) -> None:
    """Chat with a specific agent in the foreground (streaming)."""
    pool = orch.agent_pool
    rest = cmd[4:].strip() if cmd.lower().startswith("/fg") else ""
    if not rest:
        console.print("[yellow]Usage: /fg <agent> <prompt>[/]")
        console.print(f"[dim]agents: {', '.join(pool.agents.keys())}[/]")
        return
    words = rest.split(maxsplit=1)
    if len(words) < 2 or words[0].lower() not in pool.agents:
        agent = pool.route(rest)
        prompt = rest
        console.print(f"[dim]routed to agent: {agent}[/]")
    else:
        agent = words[0].lower()
        prompt = words[1].strip()

    current: str | None = None
    try:
        async for provider, chunk in pool.run(agent, prompt):
            if provider == "tool":
                console.print(f"[dim]{chunk}[/]")
                continue
            if provider == "error":
                console.print(f"[red]{chunk}[/]")
                continue
            if provider != current:
                if current is not None:
                    console.print()
                console.print(f"[dim]‹{agent}:{provider}›[/]", end=" ")
                current = provider
            print(chunk, end="", flush=True)
        print()
    except Exception as exc:
        console.print(f"[red]agent error:[/] {exc}")


def _print_jobs(orch: Orchestrator) -> None:
    pool = orch.agent_pool
    tasks = pool.list_tasks()
    if not tasks:
        console.print("[dim]no background jobs — use /bg <prompt> to spawn one[/]")
        return
    running = pool.running_count()
    table = Table(title=f"Background Jobs ({running} running)", header_style="bold cyan")
    table.add_column("id")
    table.add_column("agent")
    table.add_column("status")
    table.add_column("dur")
    table.add_column("prompt / error", overflow="fold")
    for t in tasks[:15]:
        style = {"done": "green", "failed": "red", "running": "yellow", "queued": "dim"}.get(
            t.status, "white"
        )
        body = t.prompt if not t.error else t.error
        table.add_row(
            t.task_id, t.agent, f"[{style}]{t.status}[/]", f"{t.duration_s:.0f}s", (body or "")[:60]
        )
    console.print(table)
    console.print("[dim]/result <id> for full output[/]")


def _print_result(orch: Orchestrator, cmd: str) -> None:
    parts = cmd.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        console.print("[yellow]Usage: /result <task_id>[/]")
        return
    task = orch.agent_pool.get_task(parts[1].strip())
    if task is None:
        console.print(f"[red]no job found: {parts[1].strip()}[/]")
        return
    color = {"done": "green", "failed": "red", "running": "yellow", "queued": "dim"}.get(
        task.status, "white"
    )
    console.print(
        Panel(
            f"[bold]{task.agent}[/] agent · [dim]{task.provider or '—'}[/] · {task.duration_s:.0f}s\n"
            f"prompt: {task.prompt[:120]}\n\n"
            f"status: [{color}]{task.status}[/]",
            title=f"Job {task.task_id}",
            border_style=color,
        )
    )
    if task.status == "done":
        console.print(task.result or "[dim](empty result)[/]")
    elif task.status == "failed":
        console.print(f"[red]{task.error}[/]")
    else:
        console.print("[dim]still working — check back shortly[/]")


# ==================== WORKFLOW COMMANDS ====================


def _print_flows(orch: Orchestrator) -> None:
    engine = orch.workflow_engine
    specs = engine.list()
    if not specs:
        console.print("[dim]no workflows — create with /flow compose <description>[/]")
        return
    table = Table(title=f"Workflows ({engine.stats()['workflows']})", header_style="bold cyan")
    table.add_column("id")
    table.add_column("name")
    table.add_column("trigger")
    table.add_column("steps")
    table.add_column("runs")
    table.add_column("state")
    for s in specs:
        trig = s.trigger.get("type", "manual")
        if trig == "schedule":
            trig += f" {s.trigger.get('interval_s', '?')}s"
        elif trig == "cron":
            trig += f" `{s.trigger.get('cron', '')}`"
        elif trig == "event":
            trig += f" {s.trigger.get('event', '')}"
        steps = " → ".join(st.tool for st in s.steps[:4])
        if len(s.steps) > 4:
            steps += " → …"
        runs = orch.workflow_engine.conn.execute(
            "SELECT COUNT(*) FROM workflow_runs WHERE workflow_id = ?", (s.id,)
        ).fetchone()[0]
        table.add_row(
            s.id,
            s.name,
            trig,
            steps[:44],
            str(runs),
            "[green]on[/]" if s.enabled else "[dim]off[/]",
        )
    console.print(table)
    console.print(
        "[dim]/flow run <id> [k=v …] · /flow compose <description> · /flow del <id> · /flow on|off <id>[/]"
    )


async def _do_flow(orch: Orchestrator, cmd: str) -> None:
    engine = orch.workflow_engine
    parts = cmd.split(maxsplit=3)
    if len(parts) < 2 or not parts[1].strip():
        _print_flows(orch)
        return
    sub = parts[1].lower()

    if sub == "list":
        _print_flows(orch)
    elif sub == "compose":
        desc = parts[2].strip() if len(parts) > 2 else ""
        if not desc:
            console.print("[yellow]Usage: /flow compose <description of the automation>[/]")
            return
        with console.status("[cyan]composing workflow…[/]"):
            spec, msg = await orch.flow_composer.compose_and_create(desc)
        if spec is None:
            console.print(f"[red]{msg}[/]")
            return
        console.print(f"[green]Composed workflow [bold]{spec.name}[/] ({len(spec.steps)} steps)[/]")
        for i, st in enumerate(spec.steps, 1):
            extra = []
            if st.save_as:
                extra.append(f"save=${st.save_as}")
            if st.condition:
                extra.append(f"if {st.condition}")
            if st.foreach:
                extra.append(f"foreach ${st.foreach}")
            console.print(f"  {i}. [cyan]{st.tool}[/] {json.dumps(st.args)[:60]} {' '.join(extra)}")
        trig = spec.trigger.get("type", "manual")
        console.print(f"[dim]trigger: {trig} · run with: /flow run {spec.id}[/]")
    elif sub == "run":
        rest = parts[2].strip() if len(parts) > 2 else ""
        if not rest:
            console.print("[yellow]Usage: /flow run <id> [k=v k2=v2][/]  (run in background)")
            return
        tokens = rest.split()
        wf_id = tokens[0]
        variables = {}
        for tok in tokens[1:]:
            if "=" in tok:
                k, v = tok.split("=", 1)
                variables[k] = v
        try:
            run_id = await engine.run(wf_id, variables, background=True)
            console.print(f"[green]started run [bold]{run_id}[/] of '{wf_id}' in background[/]")
            console.print("[dim]/runs to watch · /runs " + run_id + " for detail[/]")
        except Exception as exc:
            console.print(f"[red]run failed:[/] {exc}")
    elif sub == "show":
        run_id = parts[2].strip() if len(parts) > 2 else ""
        d = engine.run_detail(run_id)
        if not d:
            console.print(f"[red]run not found: {run_id}[/]")
            return
        color = {"done": "green", "failed": "red", "running": "yellow"}.get(d["status"], "white")
        console.print(
            Panel(
                f"{d['workflow_name']} · [{color}]{d['status']}[/] · {d.get('duration_s') or 0:.0f}s"
                + (f"\nerror: {d['error']}" if d.get("error") else ""),
                title=f"Run {d['id']}",
                border_style=color,
            )
        )
        for s in d.get("steps", []):
            mark = {"ok": "[green]+[/]", "error": "[red]x[/]", "skipped": "[dim]-[/]"}.get(
                s["status"], "?"
            )
            console.print(
                f"  {mark} {s['tool']} [dim]({s['duration_ms']}ms)[/] {(s['output'] or s['error'])[:80]}"
            )
    elif sub == "del":
        wf_id = parts[2].strip() if len(parts) > 2 else ""
        if engine.delete(wf_id):
            console.print(f"[green]deleted workflow {wf_id}[/]")
        else:
            console.print(f"[red]not found: {wf_id}[/]")
    elif sub in ("on", "off"):
        wf_id = parts[2].strip() if len(parts) > 2 else ""
        if engine.set_enabled(wf_id, sub == "on"):
            console.print(f"[green]workflow {wf_id} {'enabled' if sub == 'on' else 'disabled'}[/]")
        else:
            console.print(f"[red]not found: {wf_id}[/]")
    else:
        console.print(
            "[yellow]Usage: /flow [list | compose <desc> | run <id> [k=v] | show <run_id> | del <id> | on/off <id>][/]"
        )


def _print_runs(orch: Orchestrator, cmd: str) -> None:
    engine = orch.workflow_engine
    parts = cmd.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        _do_flow_sync_show(orch, parts[1].strip())
        return
    runs = engine.run_history(limit=12)
    if not runs:
        console.print("[dim]no workflow runs yet — /flow run <id> to start one[/]")
        return
    table = Table(title="Workflow Runs", header_style="bold cyan")
    table.add_column("run_id")
    table.add_column("workflow")
    table.add_column("status")
    table.add_column("dur")
    table.add_column("result / error", overflow="fold")
    for r in runs:
        color = {"done": "green", "failed": "red", "running": "yellow"}.get(r["status"], "white")
        table.add_row(
            r["id"],
            r["workflow_name"],
            f"[{color}]{r['status']}[/]",
            f"{r['duration_s']:.0f}s" if r["duration_s"] else "-",
            (r["result"] or r["error"] or "")[:70],
        )
    console.print(table)
    console.print("[dim]/runs <run_id> for step detail[/]")


def _do_flow_sync_show(orch: Orchestrator, run_id: str) -> None:
    engine = orch.workflow_engine
    d = engine.run_detail(run_id)
    if not d:
        console.print(f"[red]run not found: {run_id}[/]")
        return
    color = {"done": "green", "failed": "red", "running": "yellow"}.get(d["status"], "white")
    console.print(
        Panel(
            f"{d['workflow_name']} · [{color}]{d['status']}[/] · {d.get('duration_s') or 0:.0f}s",
            title=f"Run {d['id']}",
            border_style=color,
        )
    )
    for s in d.get("steps", []):
        mark = {"ok": "[green]+[/]", "error": "[red]x[/]", "skipped": "[dim]-[/]"}.get(
            s["status"], "?"
        )
        console.print(
            f"  {mark} {s['tool']} [dim]({s['duration_ms']}ms)[/] {(s['output'] or s['error'])[:80]}"
        )


def _do_bgmode(orch: Orchestrator, cmd: str) -> None:
    parts = cmd.split()
    arg = parts[1].lower() if len(parts) > 1 else "status"
    if arg in ("on", "off"):
        orch.auto_background = arg == "on"
    state = (
        "[green]ON[/] — heavy work (coding/projects/research) auto-dispatches to background agents; chat stays instant"
        if orch.auto_background
        else "[yellow]OFF[/] — everything runs in the foreground"
    )
    console.print(f"auto-background: {state}")


def _do_agent_cmd(orch: Orchestrator, cmd: str) -> None:
    """Custom agent management: /agent new | brain | del"""
    parts = cmd.split(maxsplit=2)
    sub = parts[1].lower() if len(parts) > 1 else ""
    rest = parts[2].strip() if len(parts) > 2 else ""

    if sub == "new":
        words = rest.split(maxsplit=1)
        if not words:
            console.print("[yellow]Usage: /agent new <name> <description>[/]")
            return
        name = words[0].lower()
        desc = words[1] if len(words) > 1 else f"custom agent {name}"
        try:
            spec = orch.agent_pool.create_agent(name, description=desc)
        except ValueError as e:
            console.print(f"[red]error:[/] {e}")
            return
        orch.brain_manager.auto_assign_all()
        brain = orch.brain_manager.get(spec.name)
        console.print(f"[green]created agent[/] {spec.icon} {spec.name} — {desc}")
        console.print(
            f"[dim]dedicated brain: {brain.primary if brain else 'full chain'} · use: /bg {spec.name} <task>[/]"
        )

    elif sub == "brain":
        tokens = rest.split()
        if not tokens or tokens[0].lower() == "show":
            rows = orch.brain_manager.all()
            if not rows:
                console.print("[dim]no brain assignments — run /agent brain auto[/]")
                return
            table = Table(title="Agent Brains", header_style="bold cyan")
            table.add_column("agent")
            table.add_column("brain")
            table.add_column("fallbacks")
            table.add_column("reason")
            for a in rows:
                table.add_row(a.agent, a.primary, ", ".join(a.fallbacks[:3]) or "—", a.reason[:30])
            console.print(table)
        elif tokens[0].lower() == "auto":
            result = orch.brain_manager.auto_assign_all()
            for agent, prov in result.items():
                console.print(f"  {agent} → [cyan]{prov}[/]")
            console.print(f"[green]{len(result)} brains auto-assigned from learned performance[/]")
        elif len(tokens) >= 2:
            agent, provider = tokens[0], tokens[1]
            known = {p.name for p in orch.cfg.providers}
            if provider not in known:
                console.print(f"[red]unknown provider '{provider}' — see /chain[/]")
                return
            brain = orch.brain_manager.assign(agent, provider, reason="manual")
            console.print(f"[green]{agent}'s brain set to[/] {brain.primary}")
        else:
            console.print("[yellow]Usage: /agent brain <agent> <provider> | auto | show[/]")

    elif sub == "del":
        if not rest:
            console.print("[yellow]Usage: /agent del <name>[/]")
            return
        if orch.agent_pool.delete_agent(rest.strip().lower()):
            console.print(f"[green]deleted custom agent {rest.strip().lower()}[/]")
        else:
            console.print("[red]not found or built-in (built-ins can't be deleted)[/]")

    else:
        console.print(
            "[yellow]Usage: /agent new <name> <desc> | brain <agent> <provider>|auto|show | del <name>[/]"
        )


# ==================== AGENT TEAM (collaboration pipelines) ====================


def _print_team(orch: Orchestrator) -> None:
    team = orch.agent_team
    table = Table(title="Agent Teams (collaboration pipelines)", header_style="bold cyan")
    table.add_column("pipeline")
    table.add_column("flow")
    table.add_column("description")
    for spec in team.pipelines.values():
        flow = " → ".join(s.agent for s in spec.steps)
        table.add_row(spec.name, flow[:52], spec.description[:40])
    console.print(table)
    active = [r for r in team.runs.values() if r.status in ("queued", "running")]
    console.print(
        f"[dim]/team run <pipeline> <task> · {len(active)} running · "
        f"auto-review: {'ON' if team.auto_review_enabled else 'OFF'}[/]"
    )


async def _do_team(orch: Orchestrator, cmd: str) -> None:
    team = orch.agent_team
    parts = cmd.split(maxsplit=2)
    sub = parts[1].lower() if len(parts) > 1 else "list"

    if sub == "list" or sub == "":
        _print_team(orch)
    elif sub == "run":
        if len(parts) < 4:
            console.print("[yellow]Usage: /team run <pipeline> <task description>[/]")
            return
        pipeline = parts[2].lower()
        task_text = parts[3].strip()
        if pipeline not in team.pipelines:
            console.print(f"[red]unknown pipeline '{pipeline}' — /team to list[/]")
            return
        try:
            run = team.start(pipeline, task_text)
        except ValueError as e:
            console.print(f"[red]error:[/] {e}")
            return
        console.print(
            f"[green]started pipeline [bold]{pipeline}[/] ({len(team.pipelines[pipeline].steps)} steps) run {run.run_id}[/]"
        )
        console.print(
            "[dim]agents will collaborate in the background — results land in chat + /jobs[/]"
        )
    elif sub == "results":
        arg = parts[2].strip() if len(parts) > 2 else ""
        if arg:
            run = team.runs.get(arg)
            if not run:
                console.print(f"[red]no pipeline run '{arg}'[/]")
                return
            color = {"done": "green", "failed": "red", "running": "yellow"}.get(run.status, "white")
            console.print(
                Panel(
                    f"{run.pipeline} · [{color}]{run.status}[/] · {run.duration_s:.0f}s",
                    title=f"Pipeline {run.run_id}",
                    border_style=color,
                )
            )
            for key, out in run.outputs.items():
                console.print(f"[cyan]── {key} ──[/]")
                console.print(out[:1200])
        else:
            runs = sorted(team.runs.values(), key=lambda r: r.created_at, reverse=True)[:10]
            if not runs:
                console.print("[dim]no pipeline runs yet — /team run <pipeline> <task>[/]")
                return
            table = Table(title="Pipeline Runs", header_style="bold cyan")
            table.add_column("run")
            table.add_column("pipeline")
            table.add_column("status")
            table.add_column("dur")
            for r in runs:
                color = {"done": "green", "failed": "red", "running": "yellow"}.get(
                    r.status, "white"
                )
                table.add_row(
                    r.run_id, r.pipeline, f"[{color}]{r.status}[/]", f"{r.duration_s:.0f}s"
                )
            console.print(table)
    elif sub == "autoreview":
        team.auto_review_enabled = not team.auto_review_enabled
        console.print(
            f"auto-review (coder → code_reviewer): {'[green]ON' if team.auto_review_enabled else '[red]OFF'}[/]"
        )
    else:
        console.print(
            "[yellow]Usage: /team list | run <pipeline> <task> | results [run_id] | autoreview[/]"
        )


# ==================== HEALTH / RELOAD / BACKUP ====================


def _print_health(orch: Orchestrator) -> None:
    lat = orch.router.ledger.latency_stats()
    table = Table(title="Provider Health", header_style="bold cyan")
    table.add_column("provider")
    table.add_column("state")
    table.add_column("lat (1st tok)", justify="right")
    table.add_column("p95", justify="right")
    table.add_column("succ/fail", justify="right")
    table.add_column("budget left", justify="right")
    for _prio, name, _model, state, left in orch.chain_status():
        p = next((x for x in orch.cfg.providers if x.name == name), None)
        succ = getattr(p, "_success_count", 0) if p else 0
        fail = getattr(p, "_failure_count", 0) if p else 0
        lat_entry = lat.get(name)
        lat_str = f"{lat_entry['first_token_avg_ms']}ms" if lat_entry else "—"
        p95_str = f"{lat_entry['first_token_p95_ms']}ms" if lat_entry else "—"
        style = (
            "green"
            if state == "READY"
            else ("red" if state.startswith(("BUDGET", "COOLING")) else "yellow")
        )
        table.add_row(name, f"[{style}]{state}[/]", lat_str, p95_str, f"{succ}/{fail}", left)
    console.print(table)
    stats = orch.workflow_engine.stats()
    console.print(
        f"[dim]workflows: {stats['workflows']} · runs: {stats['runs']} · agents: "
        f"{len(orch.agent_pool.agents)} · jobs running: {sum(1 for t in orch.agent_pool.tasks.values() if t.status == 'running')}[/]"
    )
    # P6: learned routing leaderboard
    try:
        lb = orch.routing_optimizer.leaderboard()
        if lb:
            console.print("[dim]learned routing (top per task):[/]")
            for task, entries in list(lb.items())[:6]:
                if entries:
                    top = entries[0]
                    console.print(f"[dim]  {task}: {top['provider']} ({top['score']})[/]")
    except Exception:
        pass


def _do_reload(orch: Orchestrator) -> None:
    """Hot-reload config + rebuild the provider chain in place."""
    from core.config import load_config

    try:
        fresh = load_config()
    except Exception as exc:
        console.print(f"[red]config load failed:[/] {exc}")
        return
    old_names = {p.name for p in orch.cfg.providers}
    new_names = {p.name for p in fresh.providers}
    orch.router.providers = sorted(fresh.providers, key=lambda p: p.priority)
    # restore health stats for providers that survived the reload
    for p_new in orch.router.providers:
        p_old = next((x for x in orch.cfg.providers if x.name == p_new.name), None)
        if p_old is not None:
            p_new._success_count = p_old._success_count
            p_new._failure_count = p_old._failure_count
    orch.cfg.providers = fresh.providers
    if fresh.system_prompt != orch.cfg.system_prompt:
        orch.cfg.system_prompt = fresh.system_prompt
        if orch.history and orch.history[0].get("role") == "system":
            orch.history[0]["content"] = orch.persona.build_system_prompt(fresh.system_prompt)
    added = new_names - old_names
    removed = old_names - new_names
    console.print(f"[green]config reloaded[/] — {len(fresh.providers)} providers active")
    if added:
        console.print(f"  [green]+ added:[/] {', '.join(sorted(added))}")
    if removed:
        console.print(f"  [red]- removed:[/] {', '.join(sorted(removed))}")
    print_keys()


def _do_backup(orch: Orchestrator) -> None:
    """Archive every JARVIS database + config to a timestamped zip."""
    import zipfile
    from datetime import datetime

    data_dir = orch.cfg.db_path.parent
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = data_dir / f"jarvis_backup_{stamp}.zip"
    targets = [
        data_dir / "usage.db",
        data_dir / "memory.db",
        data_dir / "knowledge.db",
        data_dir / "conversations.db",
        data_dir / "workflows.db",
        data_dir / "events.db",
        data_dir / "automation.db",
        data_dir / "api_tokens.db",
        data_dir / "scheduler.db",
        # NOTE: .env deliberately excluded (live secrets). Re-create by hand on restore.
        Path("config.yaml"),
        Path.home() / ".jarvis_persona.json",
        Path.home() / ".jarvis_preferences.json",
    ]
    saved = 0
    with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for t in targets:
            if t and t.exists():
                zf.write(t, arcname=t.name)
                saved += 1
        # ML models pickle if present
        ml = data_dir / "ml_models" / "models.pkl"
        if ml.exists():
            zf.write(ml, arcname="ml_models.pkl")
            saved += 1
    console.print(
        f"[green]backup created[/] → {backup_path} ({saved} files, {backup_path.stat().st_size / 1024:.0f} KB)"
    )


# ── Inline-command handlers (formerly embedded in the REPL dispatch chain) ──


def _do_reset(orch: Orchestrator, cmd: str) -> None:
    orch.reset()
    console.print("[dim]session memory wiped[/]")


def _do_new(orch: Orchestrator, cmd: str) -> None:
    sid = orch.start_new_session()
    console.print(f"[green]New conversation started ({sid})[/]")


def _do_resume(orch: Orchestrator, cmd: str) -> None:
    sid = cmd.split(" ", 1)[1].strip()
    msgs = orch.resume_session(sid)
    console.print(f"[green]Resumed {len(msgs)} messages from session {sid}[/]")


def _do_tools(orch: Orchestrator, cmd: str) -> None:
    console.print("[green]available tools:[/] " + ", ".join(orch.toolbox.names()))


async def _do_forget(orch: Orchestrator, cmd: str) -> None:
    arg = cmd[8:].strip()
    if not arg:
        console.print("[yellow]Usage: /forget <query>[/]")
    else:
        console.print(
            await orch.toolbox.execute("forget", json.dumps({"query": arg})),
            markup=False,
            highlight=False,
        )


async def _do_consolidate(orch: Orchestrator, cmd: str) -> None:
    console.print("[dim]consolidating memory (promote + report, no deletes)...[/]")
    console.print(
        await orch.toolbox.execute("memory_consolidate", "{}"),
        markup=False,
        highlight=False,
    )


async def _do_init(orch: Orchestrator, cmd: str) -> None:
    arg = cmd[5:].strip() or "."
    console.print("[dim]initializing (fast, no index)...[/]")
    # Raw tool output may contain [...] that Rich would parse as markup
    console.print(
        await orch.toolbox.execute("dev_init", json.dumps({"path": arg})),
        markup=False,
        highlight=False,
    )


async def _do_review(orch: Orchestrator, cmd: str) -> None:
    arg = cmd[7:].strip() or "."
    console.print(
        await orch.toolbox.execute("dev_review", json.dumps({"path": arg})),
        markup=False,
        highlight=False,
    )


async def _do_test(orch: Orchestrator, cmd: str) -> None:
    rest = cmd[5:].strip()
    parts = rest.split(maxsplit=1) if rest else []
    if len(parts) == 0:
        payload = {}
    elif len(parts) == 1:
        payload = {"path": parts[0]}
    else:
        payload = {"path": parts[0], "cmd": parts[1]}
    console.print(
        await orch.toolbox.execute("dev_test", json.dumps(payload)),
        markup=False,
        highlight=False,
    )


async def _do_pr(orch: Orchestrator, cmd: str) -> None:
    rest = cmd[3:].strip()
    if not rest:
        console.print("[yellow]Usage: /pr <title> | [body][/]")
    else:
        title, _, body = rest.partition("|")
        console.print(
            await orch.toolbox.execute(
                "dev_pr", json.dumps({"title": title.strip(), "body": body.strip()})
            ),
            markup=False,
            highlight=False,
        )


async def _do_standup(orch: Orchestrator, cmd: str) -> None:
    arg = cmd[8:].strip() or "."
    console.print(
        await orch.toolbox.execute("dev_standup", json.dumps({"path": arg})),
        markup=False,
        highlight=False,
    )


async def _do_triage(orch: Orchestrator, cmd: str) -> None:
    arg = cmd[7:].strip().lower()
    if arg == "sort":
        console.print("[yellow]Sorting Downloads into _sorted_*/ (real move)...[/]")
        console.print(
            await orch.toolbox.execute("inbox_sort", '{"dry_run": false}'),
            markup=False,
            highlight=False,
        )
    else:
        console.print(
            await orch.toolbox.execute("inbox_report", "{}"),
            markup=False,
            highlight=False,
        )
        console.print("[dim]Dry-run report only. `/triage sort` moves files.[/]")


async def _do_wa(orch: Orchestrator, cmd: str) -> None:
    rest = cmd[3:].strip()
    sub, _, arg2 = rest.partition(" ")
    sub = sub.lower()
    if sub in ("triage", ""):
        console.print(await orch.toolbox.execute("wa_triage", "{}"), markup=False, highlight=False)
    elif sub == "chats":
        console.print(await orch.toolbox.execute("wa_chats", "{}"), markup=False, highlight=False)
    elif sub == "read" and arg2.strip():
        console.print(
            await orch.toolbox.execute("wa_read", json.dumps({"chat": arg2.strip()})),
            markup=False,
            highlight=False,
        )
    elif sub == "send" and arg2.strip():
        console.print(
            await orch.toolbox.execute("wa_send_draft", json.dumps({"id": arg2.strip()})),
            markup=False,
            highlight=False,
        )
    elif sub == "send":
        console.print(
            await orch.toolbox.execute("wa_send_draft", "{}"),
            markup=False,
            highlight=False,
        )
    elif sub == "contact":
        _cparts = (arg2 or "").strip().split(None, 2)
        _cact = _cparts[0].lower() if _cparts else "list"
        _cname = _cparts[1] if len(_cparts) > 1 else ""
        _cchat = _cparts[2] if len(_cparts) > 2 else ""
        console.print(
            await orch.toolbox.execute(
                "wa_contact",
                json.dumps({"action": _cact, "name": _cname, "chat": _cchat}),
            ),
            markup=False,
            highlight=False,
        )
    elif sub == "sendto" and arg2.strip():
        _m2 = re.match(r"(\+?\d[\d\s\-]{7,})\s+(.*)", arg2.strip(), re.DOTALL)
        if not _m2:
            console.print("[yellow]Usage: /wa sendto <number> <text>[/]")
        else:
            console.print(
                await orch.toolbox.execute(
                    "wa_send",
                    json.dumps({"chat": _m2.group(1), "text": _m2.group(2)}),
                ),
                markup=False,
                highlight=False,
            )
    else:
        console.print(
            "[yellow]Usage: /wa triage|chats|read <chat>|send <id>|sendto <number> <text>[/]"
        )


# ── Voice commands: the lazy VoiceSession factory stays in jarvis.main() (it
# closes over cfg/console/orch), so it is injected at startup ──

_VOICE_SESSION_GETTER = None


def set_voice_session_getter(getter) -> None:
    """jarvis.main() injects its lazy VoiceSession factory before the REPL."""
    global _VOICE_SESSION_GETTER
    _VOICE_SESSION_GETTER = getter


def _voice_session():
    if _VOICE_SESSION_GETTER is None:
        raise RuntimeError("voice session getter not set (set_voice_session_getter)")
    return _VOICE_SESSION_GETTER()


async def _do_voice(orch: Orchestrator, cmd: str) -> bool | None:
    try:
        await _voice_session().run_push_to_talk()
        return True
    except ImportError as exc:
        console.print(f"[red]voice deps missing:[/] {exc}")
        console.print("run: pip install sounddevice faster-whisper edge-tts miniaudio")
        # Pre-existing behavior: missing voice deps fall through to the brain.
        return False


def _do_tts(orch: Orchestrator, cmd: str) -> None:
    vs = _voice_session()
    arg = cmd[4:].strip()
    if arg in ("on", "off"):
        vs.speaker.enabled = arg == "on"
    try:
        _eng = vs.speaker._pick_engine()
        _vox = (
            getattr(vs.speaker, "kokoro_voice", "")
            if _eng == "kokoro"
            else getattr(vs.speaker, "edge_voice", "")
        )
        _desc = f"{_eng}/{_vox}" if _vox else _eng
    except Exception:
        _desc = "?"
    console.print(f"[green]tts is {'ON' if vs.speaker.enabled else 'OFF'}[/] (voice: {_desc})")


async def _do_doctor(orch: Orchestrator, cmd: str) -> None:
    console.print("[dim]running voice self-check...[/]")
    console.print(await orch.toolbox.execute("voice_doctor", "{}"), markup=False, highlight=False)


def _do_alerts(orch: Orchestrator, cmd: str) -> None:
    arg = cmd[7:].strip().lower()
    if arg == "on":
        orch.voice_alerts = True
    elif arg == "off":
        orch.voice_alerts = False
    from core.voice_alerts import alerts_enabled, parse_quiet

    _on = alerts_enabled(orch.voice_alerts)
    _q = parse_quiet()
    console.print(
        f"[green]voice alerts {'ON' if _on else 'OFF'}[/]"
        f" (quiet hours {(_q[0] if _q else '?')}-{(_q[1] if _q else '?')}, "
        f"override: {orch.voice_alerts})"
    )


async def run_routine_tool(orch: Orchestrator, con, tool: str) -> None:
    """Run a morning/evening routine bundle and print the result.

    Single copy of the exec + error wrapper, shared by the /morning and
    /evening registry entries and by the NL routine shortcut in
    skills/nl_shortcuts.py (consolidation step 3 dedup).
    """
    try:
        out = await orch.toolbox.execute(tool, "{}")
    except Exception as exc:
        out = f"Routine error: {exc}"
    con.print(out, markup=False, highlight=False)


async def _do_routine(orch: Orchestrator, cmd: str) -> None:
    await run_routine_tool(orch, console, "morning_start" if cmd == "/morning" else "evening_wrap")


# ── The slash-command table ──
#
# Ordered: first match wins, exactly like the if/elif chain this replaces.
# Order matters: /agents must beat /agent, /plugins must beat /plugin.
# NOTE: /bgmode is unreachable — /bg (prefix) matches it first. Pre-existing
# shadow preserved verbatim; fixing it is out of scope for this pure move.


@dataclass(frozen=True)
class SlashCommand:
    """One slash command: name/prefix + handler(orch, cmd).

    mode: "exact"  — cmd == name
          "word"   — cmd == name or cmd.startswith(name + " ")
          "prefix" — cmd.startswith(name)
    Handler may be sync or async. Returning False means "not handled — fall
    through to the NL shortcuts / brain" (used by /voice on missing deps).
    """

    name: str
    handler: Callable
    mode: str = "exact"

    def matches(self, cmd: str) -> bool:
        if self.mode == "exact":
            return cmd == self.name
        if self.mode == "word":
            return cmd == self.name or cmd.startswith(self.name + " ")
        return cmd.startswith(self.name)


SLASH_COMMANDS: list[SlashCommand] = [
    SlashCommand("/reset", _do_reset),
    SlashCommand("/chain", print_chain),
    SlashCommand("/approve", _do_approve),
    SlashCommand("/reject", _do_reject),
    SlashCommand("/pending", _print_pending),
    SlashCommand("/approve_all", _do_approve_all),
    SlashCommand("/history", _print_history),
    SlashCommand("/new", _do_new),
    SlashCommand("/resume ", _do_resume, "prefix"),
    SlashCommand("/stats", _print_stats),
    SlashCommand("/keys", print_keys),
    SlashCommand("/tools", _do_tools),
    SlashCommand("/memory", _print_memory),
    SlashCommand("/forget ", _do_forget, "prefix"),
    SlashCommand("/consolidate", _do_consolidate),
    SlashCommand("/tasks", _print_tasks),
    SlashCommand("/automation", _print_automation),
    SlashCommand("/rule", _do_rule, "word"),
    SlashCommand("/proactive", _print_proactive),
    SlashCommand("/briefing", _do_briefing),
    SlashCommand("/init", _do_init, "word"),
    SlashCommand("/review", _do_review, "word"),
    SlashCommand("/test", _do_test, "word"),
    SlashCommand("/pr", _do_pr, "word"),
    SlashCommand("/standup", _do_standup, "word"),
    SlashCommand("/triage", _do_triage, "word"),
    SlashCommand("/wa", _do_wa, "word"),
    SlashCommand("/vision", _print_vision),
    SlashCommand("/notify", _do_notify),
    SlashCommand("/ml", _do_ml),
    SlashCommand("/persona", _do_persona),
    SlashCommand("/preferences", _do_preferences),
    SlashCommand("/dialogue", _do_dialogue),
    SlashCommand("/summarizer", _do_summarizer),
    SlashCommand("/agents", _print_agents),
    SlashCommand("/bg", _do_bg, "prefix"),
    SlashCommand("/fg", _do_fg, "prefix"),
    SlashCommand("/jobs", _print_jobs),
    SlashCommand("/result", _print_result, "prefix"),
    SlashCommand("/flow", _do_flow, "prefix"),
    SlashCommand("/runs", _print_runs, "prefix"),
    SlashCommand("/health", _print_health),
    SlashCommand("/reload", _do_reload),
    SlashCommand("/backup", _do_backup),
    # Unreachable: "/bg" prefix matches "/bgmode ..." first (pre-existing).
    SlashCommand("/bgmode", _do_bgmode, "prefix"),
    SlashCommand("/agent", _do_agent_cmd, "prefix"),
    SlashCommand("/team", _do_team, "prefix"),
    SlashCommand("/plugins", _print_plugins),
    SlashCommand("/a2a", _print_a2a),
    SlashCommand("/plugin ", _do_plugin, "prefix"),
    SlashCommand("/screenshot", _do_screenshot, "prefix"),
    SlashCommand("/ocr", _do_ocr, "prefix"),
    SlashCommand("/readscreen", _do_read_screen, "prefix"),
    SlashCommand("/analyze", _do_analyze, "prefix"),
    SlashCommand("/voice", _do_voice),
    SlashCommand("/tts", _do_tts, "prefix"),
    SlashCommand("/doctor", _do_doctor),
    SlashCommand("/alerts", _do_alerts, "prefix"),
    SlashCommand("/morning", _do_routine),
    SlashCommand("/evening", _do_routine),
]


async def dispatch_slash(orch: Orchestrator, cmd: str) -> bool:
    """Dispatch a REPL input through the slash-command table.

    Returns True when the input was consumed (unknown /commands print the
    help text and count as consumed). False = fall through to the NL
    shortcuts (mail/routine/pc) and then the brain.

    Arity-tolerant: legacy display handlers take (orch) or () while newer
    ones take (orch, cmd) — inspect and pass what each accepts. A handler
    mismatch used to raise TypeError out of the REPL loop and drop the
    whole session into teardown (every slash command "stopping the mesh").
    """
    for entry in SLASH_COMMANDS:
        if entry.matches(cmd):
            result = _call_handler(entry.handler, orch, cmd)
            if inspect.isawaitable(result):
                result = await result
            return result is not False
    if cmd.startswith("/"):
        # markup OFF: help text contains literal [/...] fragments that rich
        # would read as closing tags (MarkupError -> dead session).
        console.print(get_help(), markup=False, highlight=False)
        return True
    return False


def _call_handler(handler, orch: Orchestrator, cmd: str):
    """Call a slash handler with the arity it accepts.

    (orch, cmd) preferred; falls back to (orch) then () for legacy
    display handlers. Lets a handler's OWN TypeError propagate (real bug)
    instead of masking it as an arity problem.
    """
    try:
        params = list(inspect.signature(handler).parameters.values())
    except (TypeError, ValueError):
        return handler(orch, cmd)
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    has_var_positional = any(p.kind == p.VAR_POSITIONAL for p in params)
    if has_var_positional or len(positional) >= 2:
        return handler(orch, cmd)
    if len(positional) == 1:
        return handler(orch)
    return handler()
