"""Phase 3 inbox triage: dry-run by default, sorts Downloads by type. Safe."""

from __future__ import annotations

import shutil
from pathlib import Path

from core.tools import Tool, ToolBox

RULES = {
    "images": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"},
    "docs": {".pdf", ".docx", ".doc", ".txt", ".md", ".xlsx", ".csv", ".pptx"},
    "installers": {".exe", ".msi", ".zip", ".7z", ".rar", ".tar", ".gz"},
    "media": {".mp3", ".wav", ".mp4", ".mkv", ".avi", ".mov"},
}


def _inbox() -> Path:
    return Path.home() / "Downloads"


def triage_report(limit: int = 20) -> str:
    inbox = _inbox()
    if not inbox.exists():
        return "Downloads folder not found."
    counts: dict[str, int] = {}
    samples: dict[str, list[str]] = {}
    for f in inbox.iterdir():
        if not f.is_file():
            continue
        cat = next((c for c, exts in RULES.items() if f.suffix.lower() in exts), "other")
        counts[cat] = counts.get(cat, 0) + 1
        samples.setdefault(cat, []).append(f.name)
    if not counts:
        return "Downloads is empty. Nothing to triage."
    lines = [f"Inbox ({sum(counts.values())} files):"]
    for cat, n in sorted(counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {cat}: {n} (e.g. {', '.join(samples[cat][:3])})")
    return "\n".join(lines)[:2000]


def triage_sort(dry_run: bool = True, limit: int = 50) -> str:
    inbox = _inbox()
    if not inbox.exists():
        return "Downloads folder not found."
    moved: list[str] = []
    skipped = 0
    for f in sorted(inbox.iterdir()):
        if not f.is_file() or len(moved) >= limit:
            continue
        cat = next((c for c, exts in RULES.items() if f.suffix.lower() in exts), None)
        if cat is None:
            skipped += 1
            continue
        dest = inbox / f"_sorted_{cat}" / f.name
        if dry_run:
            moved.append(f"{f.name} -> _sorted_{cat}/")
        else:
            try:
                dest.parent.mkdir(exist_ok=True)
                if dest.exists():
                    skipped += 1
                    continue
                shutil.move(str(f), str(dest))
                moved.append(f"{f.name} -> _sorted_{cat}/")
            except Exception:
                skipped += 1
    head = "DRY-RUN — nothing moved. " if dry_run else "Sorted. "
    out = head + f"{len(moved)} files planned/moved, {skipped} skipped."
    if moved:
        out += "\n" + "\n".join(moved[:limit])
    return out[:3000]


def register_triage(box: ToolBox):
    async def inbox_report() -> str:
        return triage_report()

    async def inbox_sort(dry_run: bool = True) -> str:
        return triage_sort(dry_run=dry_run)

    box.register(
        Tool(
            name="inbox_report",
            description="Summarize Downloads by type (safe, read-only).",
            parameters={"type": "object", "properties": {}},
            handler=inbox_report,
        )
    )
    box.register(
        Tool(
            name="inbox_sort",
            description="Sort Downloads into _sorted_<type>/ folders. dry_run=true default (safe).",
            parameters={
                "type": "object",
                "properties": {"dry_run": {"type": "boolean", "default": True}},
            },
            handler=inbox_sort,
        )
    )
