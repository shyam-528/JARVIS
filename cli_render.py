"""CLI output polish (stage 1): markdown, diffs, tool chunks.

Pure rendering helpers for the jarvis.py REPL. Everything here is
display-only and never raises — worst case the raw text is printed.
"""

from __future__ import annotations

import contextlib
import re

_MD_FENCE = "```"
_MD_HEADING = re.compile(r"(?m)^#{1,6}\s+\S")
_MD_BOLD = re.compile(r"\*\*\S.+\S\*\*")
_MD_LIST = re.compile(r"(?m)^\s*(?:[-*+]\s+\S|\d+\.\s+\S)")
_MD_TABLE = re.compile(r"(?m)^\|.*\|\s*$")

_DIFF_MARKERS = ("+++ ", "--- ", "@@ ", "diff --git ")


def looks_like_markdown(text: str | None) -> bool:
    """Heuristic: does this reply deserve rich Markdown rendering?"""
    try:
        t = str(text or "")
        if not t or len(t) < 4:
            return False
        if _MD_FENCE in t:
            return True
        if _MD_HEADING.search(t) is not None:
            return True
        if _MD_BOLD.search(t) is not None:
            return True
        if _MD_LIST.search(t) is not None:
            return True
        return len(_MD_TABLE.findall(t)) >= 2
    except Exception:
        return False


def looks_like_diff(text: str | None) -> bool:
    """Heuristic: does this tool chunk contain a unified diff?"""
    try:
        return any(line.strip().startswith(_DIFF_MARKERS) for line in str(text or "").splitlines())
    except Exception:
        return False


def render_reply(console, text: str) -> None:
    """Print a finished reply: Markdown when it looks like Markdown,
    verbatim otherwise. Never raises."""
    try:
        if looks_like_markdown(text):
            from rich.markdown import Markdown

            console.print(Markdown(str(text)))
        else:
            console.print(str(text), markup=False, highlight=False)
    except Exception:
        with contextlib.suppress(Exception):
            print(str(text))


def render_tool_chunk(console, chunk: str) -> None:
    """Print one tool-chunk line: diffs get red/green syntax color,
    everything else stays dim. Never raises."""
    try:
        if looks_like_diff(chunk):
            from rich.syntax import Syntax

            console.print(Syntax(str(chunk), "diff", theme="monokai", word_wrap=True))
        else:
            console.print(f"[dim]{chunk}[/]")
    except Exception:
        with contextlib.suppress(Exception):
            console.print(str(chunk), markup=False, highlight=False)


def print_help_table(console, help_text: str) -> None:
    """Render the slash-command wall as a compact grouped table.

    Parses the canonical HELP string (single source — no drift) into
    category rows. Falls back to raw text on any parse trouble.
    Never raises.
    """
    try:
        groups: list[list[str]] = []
        for line in str(help_text or "").splitlines():
            if not line.strip() or line.startswith("Available"):
                continue
            m = re.match(r"^  (\S[^:]*):\s*(.*)$", line)
            if m:
                groups.append([m.group(1).strip(), m.group(2).strip()])
            elif groups:
                groups[-1][1] = f"{groups[-1][1]} {line.strip()}".strip()
        if not groups:
            raise ValueError("no groups parsed")
        from rich.table import Table

        table = Table(title="commands", header_style="bold cyan", show_header=False)
        table.add_column("group", style="bold cyan", no_wrap=True)
        table.add_column("cmds", style="magenta")
        for name, cmds in groups:
            table.add_row(name, cmds)
        console.print(table)
    except Exception:
        with contextlib.suppress(Exception):
            console.print(str(help_text), markup=False, highlight=False)


def print_services(console, rows: list) -> None:
    """One services table for boot bring-up: [(name, ok, detail)].

    ok True=green, False=red, None=yellow. Display-only, never raises.
    """
    try:
        if not rows:
            return
        from rich.table import Table

        table = Table(title="services", header_style="bold cyan", show_header=False)
        table.add_column("", width=2)
        table.add_column("service", style="bold")
        table.add_column("detail", style="dim")
        for name, ok, detail in rows:
            dot = {True: "[green]●[/]", False: "[red]●[/]"}.get(ok, "[yellow]●[/]")
            table.add_row(dot, str(name), str(detail or ""))
        console.print(table)
    except Exception:
        pass
