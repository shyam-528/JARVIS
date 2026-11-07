"""Memory consolidation: promote durable facts to preferences, flag trivia.

LLM-free and deterministic (no provider spend). NEVER auto-deletes:
promotion only re-tags memories kind='promoted'; review candidates are
reported for the owner to `forget` explicitly. Runs weekly via scheduler
plus on-demand `/consolidate` and the `memory_consolidate` tool.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta


def consolidate(memory, preferences, stale_days: int = 90) -> dict:
    """Scan all memories. Returns stats dict (also human-readable via format)."""
    rows = memory.all(limit=100000)
    scanned = len(rows)
    promoted: list[str] = []
    now = datetime.now()
    cutoff = now - timedelta(days=stale_days)

    for mid, _ts, text in rows:
        try:
            found = preferences.extract_from_message(text)
        except Exception:
            found = []
        real = [p for p in found if p.confidence >= 0.8]
        if real:
            for pref in real:
                with contextlib.suppress(Exception):
                    preferences.learn(pref)
            with contextlib.suppress(Exception):
                memory.conn.execute(
                    "UPDATE memories SET kind = 'promoted' WHERE id = ? AND kind != 'promoted'",
                    (mid,),
                )
                promoted.append(text[:80])
    with contextlib.suppress(Exception):
        memory.conn.commit()

    # Review candidates: old + short + never promoted. Owner decides.
    review: list[str] = []
    for mid, ts, text in rows:
        try:
            when = datetime.strptime(ts, "%Y-%m-%d %H:%M")
        except Exception:
            continue
        if when < cutoff and len(text) < 40:
            try:
                kind = memory.conn.execute(
                    "SELECT kind FROM memories WHERE id = ?", (mid,)
                ).fetchone()[0]
            except Exception:
                kind = "fact"
            if kind == "fact":
                review.append(f"[{mid}] {text[:60]}")

    try:
        prefs_total = len(preferences.list_all())
    except Exception:
        prefs_total = -1
    return {
        "scanned": scanned,
        "promoted": len(promoted),
        "promoted_samples": promoted[:5],
        "review_candidates": len(review),
        "review_samples": review[:8],
        "preferences_total": prefs_total,
    }


def format_stats(stats: dict) -> str:
    lines = [
        f"Consolidation: scanned {stats['scanned']}, promoted {stats['promoted']} "
        f"to preferences ({stats['preferences_total']} total).",
    ]
    if stats["promoted_samples"]:
        lines.append("Promoted:")
        lines += [f"  - {s}" for s in stats["promoted_samples"]]
    if stats["review_candidates"]:
        lines.append(
            f"{stats['review_candidates']} stale trivia candidates — "
            f"`/forget <query>` to drop what you don't need:"
        )
        lines += [f"  - {s}" for s in stats["review_samples"]]
    else:
        lines.append("No stale trivia. Memory is clean, sir.")
    return "\n".join(lines)[:2500]
