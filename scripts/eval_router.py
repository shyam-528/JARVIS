"""Phase 3 router eval: one tiny no-tool prompt per active provider.

Replaces test_all_apis / test_zen / test_providers* (live-billing scripts).
LIVE NETWORK + spends a few tokens per provider. Run: python scripts/eval_router.py [--only NAME] [--timeout 20]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import load_config
from core.providers import OpenAICompatClient

PROMPT = [{"role": "user", "content": "Reply with exactly: ok"}]


async def probe(client, base_url, key, model, timeout):
    t0 = time.monotonic()
    try:
        chunks = []
        async for item in client.stream_chat(base_url, key, model, PROMPT, tools=None):
            if isinstance(item, str):
                chunks.append(item)
            if time.monotonic() - t0 > timeout:
                return {"status": "TIMEOUT", "ms": int((time.monotonic() - t0) * 1000), "reply": ""}
        ms = int((time.monotonic() - t0) * 1000)
        return {"status": "OK", "ms": ms, "reply": "".join(chunks)[:80]}
    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        name = type(exc).__name__
        return {"status": name.upper(), "ms": ms, "reply": str(exc)[:120]}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="substring filter on provider name")
    ap.add_argument("--timeout", type=float, default=25.0)
    args = ap.parse_args()

    cfg = load_config()
    providers = [p for p in cfg.providers if not args.only or args.only in p.name]
    print(f"probing {len(providers)} providers (no tools, tiny prompt)...\n")
    client = OpenAICompatClient()
    rows = []
    try:
        for p in providers:
            r = await probe(client, p.base_url, p.api_key, p.model, args.timeout)
            rows.append((p.name, p.model, r))
            tools = "tools" if p.supports_tools else "no-tools"
            print(f"{p.name:28s} {r['status']:16s} {r['ms']:6d}ms [{tools}] {r['reply'][:70]}")
    finally:
        await client.aclose()
    ok = sum(1 for _, _, r in rows if r["status"] == "OK")
    print(f"\n{ok}/{len(rows)} OK")


if __name__ == "__main__":
    asyncio.run(main())
