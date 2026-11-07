"""Storm-proof answers: when every brain is 429/down, take the request and
deliver it to Telegram + voice when quota returns, instead of failing.

File-backed queue in data_dir (storm_queue.json) so it survives reboots.
Two processes (CLI + API child) both run the drain job: items are CLAIMED
(queued -> inflight) in a single write before answering, so a second
drainer skips fresh inflight items instead of double-delivering.

Guards ($0, calm mornings): max 20 pending, max 3 answered per drain,
24h expiry, identical-text dedupe, router budgets/breakers still enforced
(the drain answers through the normal router, never force-calling).
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from pathlib import Path

from utils.json_utils import load_json_store, save_json_atomic

MAX_PENDING = 20
MAX_PER_DRAIN = 3
MAX_AGE_H = 24
MAX_ATTEMPTS = 10
INFLIGHT_STALE_S = 15 * 60


def should_queue(exc: BaseException) -> bool:
    """True only for total brain exhaustion (not for ordinary bugs)."""
    try:
        from .router import AllProvidersDown

        if isinstance(exc, AllProvidersDown):
            return True
    except Exception:
        pass
    return type(exc).__name__ == "AllProvidersDown"


class StormQueue:
    """Persistent answer queue. All methods never raise."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self._path = self.data_dir / "storm_queue.json"

    # ---------- persistence ----------

    def _load(self) -> list[dict]:
        # P0: shared corrupt-file contract — a corrupt queue is quarantined
        # aside (queued turns are user requests; silently dropping them is
        # data loss), never silently emptied.
        data = load_json_store(self._path, default={}, owner="storm_queue", expect=dict)
        items = data.get("items", [])
        return [i for i in items if isinstance(i, dict)]

    def _save(self, items: list[dict]) -> None:
        save_json_atomic(self._path, {"items": items[-100:]})

    # ---------- enqueue ----------

    def enqueue(self, surface: str, text: str) -> dict:
        """Queue one failed turn. Returns {"queued": bool, "id"/"reason"}."""
        clean = " ".join(str(text or "").split())
        if len(clean) < 2:
            return {"queued": False, "reason": "empty"}
        if clean.startswith("/"):
            return {"queued": False, "reason": "commands are not queued"}
        if len(clean) > 2000:
            clean = clean[:2000]
        items = self._load()
        now = datetime.now()
        self._expire(items, now)
        for it in items:
            if it.get("status") in ("queued", "inflight") and it.get("text", "") == clean:
                return {"queued": True, "id": it["id"], "reason": "duplicate"}
        pending = [i for i in items if i.get("status") in ("queued", "inflight")]
        if len(pending) >= MAX_PENDING:
            return {"queued": False, "reason": f"queue full ({MAX_PENDING})"}
        item = {
            "id": "q" + uuid.uuid4().hex[:7],
            "ts": now.isoformat(timespec="seconds"),
            "surface": str(surface or "?")[:12],
            "text": clean,
            "status": "queued",
            "attempts": 0,
        }
        items.append(item)
        self._save(items)
        return {"queued": True, "id": item["id"], "reason": "queued"}

    # ---------- drain-side ----------

    def _expire(self, items: list[dict], now: datetime) -> int:
        """Mark over-age / over-attempt items expired. Returns count."""
        n = 0
        for it in items:
            if it.get("status") not in ("queued", "inflight"):
                continue
            try:
                age_h = (now - datetime.fromisoformat(str(it.get("ts", "")))).total_seconds() / 3600
            except (ValueError, TypeError):
                age_h = MAX_AGE_H + 1
            if age_h > MAX_AGE_H or int(it.get("attempts", 0) or 0) >= MAX_ATTEMPTS:
                it["status"] = "expired"
                n += 1
        return n

    def claim(self, limit: int = MAX_PER_DRAIN) -> list[dict]:
        """Atomically move up to `limit` due items to inflight. Returns them."""
        items = self._load()
        now = datetime.now()
        self._expire(items, now)
        try:
            now_ts = time.time()
        except Exception:
            now_ts = 0.0
        due = []
        for it in items:
            st = it.get("status")
            if st == "queued":
                due.append(it)
            elif st == "inflight":
                try:
                    claimed = float(it.get("claimed_ts", 0) or 0)
                except (ValueError, TypeError):
                    claimed = 0.0
                if now_ts - claimed > INFLIGHT_STALE_S:
                    it["status"] = "queued"  # previous drainer died: requeue
                    due.append(it)
            if len(due) >= limit:
                break
        for it in due:
            it["status"] = "inflight"
            it["claimed_ts"] = time.time()
            it["attempts"] = int(it.get("attempts", 0) or 0) + 1
        if due:
            self._save(items)
        else:
            # still persist expiries pruned above
            self._save(items)
        return [dict(d) for d in due]

    def release(self, item_id: str) -> None:
        """Give up a claim without delivering (brains still down)."""
        items = self._load()
        for it in items:
            if it.get("id") == item_id and it.get("status") == "inflight":
                it["status"] = "queued"
                break
        self._save(items)

    def mark_delivered(self, item_id: str, preview: str = "") -> None:
        items = self._load()
        for it in items:
            if it.get("id") == item_id:
                it["status"] = "delivered"
                it["delivered_ts"] = datetime.now().isoformat(timespec="seconds")
                it["answer"] = str(preview or "")[:300]
                break
        self._save(items)

    # ---------- status / admin ----------

    def pending(self) -> list[dict]:
        items = self._load()
        self._expire(items, datetime.now())
        return [i for i in items if i.get("status") in ("queued", "inflight")]

    def status(self) -> dict:
        items = self.pending()
        oldest = min((i.get("ts", "?") for i in items), default=None)
        return {"pending": len(items), "oldest": oldest}

    def drop(self, item_id: str) -> bool:
        items = self._load()
        kept = [i for i in items if i.get("id") != item_id]
        if len(kept) == len(items):
            return False
        self._save(kept)
        return True

    def clear(self) -> int:
        items = self._load()
        n = len([i for i in items if i.get("status") in ("queued", "inflight")])
        self._save([i for i in items if i.get("status") not in ("queued", "inflight")])
        return n
