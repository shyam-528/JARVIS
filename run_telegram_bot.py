import asyncio
import contextlib
from pathlib import Path

from core.config import load_config
from core.orchestrator import Orchestrator
from core.telegram import TelegramBot


async def main():
    cfg = load_config()
    if not cfg.telegram.enabled or not cfg.telegram.bot_token:
        raise SystemExit(
            "Telegram disabled or TELEGRAM_BOT_TOKEN missing (.env). Set TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID."
        )
    from core.single_owner import acquire, read_owner, release

    base = cfg.db_path.parent
    if not acquire(base, "standalone"):
        cur = read_owner(base)
        raise SystemExit(
            f"Telegram owned by {cur} — standalone polling skipped (single owner). Stop the owner first."
        )
    orch = Orchestrator(cfg)
    await orch.start()

    bot = TelegramBot(cfg.telegram.bot_token, orch, Path("data/telegram_bot_state.json"))

    username = await bot.start()
    print(f"Bot @{username} started! Press Ctrl+C to stop.")

    # Keep running
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        print("\nStopping bot...")
    finally:
        with contextlib.suppress(Exception):
            await bot.stop()
        try:
            from core.single_owner import release

            release(cfg.db_path.parent)
        except Exception:
            pass
        with contextlib.suppress(Exception):
            await orch.aclose()


if __name__ == "__main__":
    asyncio.run(main())
