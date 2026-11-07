"""Tests for the session-unlock greeting (roadmap step 1)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

import pytest

import core.session_greeting as sg
from core.session_greeting import UnlockGreeter, build_greeting_text, in_quiet_hours


def _greeter(**kw):
    kw.setdefault("locked_fn", lambda: False)
    return UnlockGreeter(on_unlock=lambda: None, **kw)


class TestQuietHours:
    def test_overnight_window_wraps(self):
        assert in_quiet_hours(datetime(2026, 1, 1, 23), "22-7") is True
        assert in_quiet_hours(datetime(2026, 1, 1, 3), "22-7") is True
        assert in_quiet_hours(datetime(2026, 1, 1, 12), "22-7") is False
        assert in_quiet_hours(datetime(2026, 1, 1, 7), "22-7") is False

    def test_same_day_window(self):
        assert in_quiet_hours(datetime(2026, 1, 1, 13), "12-14") is True
        assert in_quiet_hours(datetime(2026, 1, 1, 15), "12-14") is False

    def test_bad_spec_never_quiet(self):
        assert in_quiet_hours(datetime(2026, 1, 1, 3), "garbage") is False
        assert in_quiet_hours(datetime(2026, 1, 1, 3), "") is False

    def test_equal_bounds_disabled(self):
        assert in_quiet_hours(datetime(2026, 1, 1, 22), "22-22") is False


class TestRateLimit:
    def test_first_unlock_fires(self):
        # _last_fire starts at 0; the real monotonic clock is way past the
        # interval, so the first unlock after boot always fires.
        g = _greeter(min_interval_min=60)
        assert g.should_fire(time.monotonic()) is True

    def test_second_unlock_within_hour_suppressed(self):
        g = _greeter(min_interval_min=60)
        now = time.monotonic()
        g._last_fire = now
        assert g.should_fire(now + 3599) is False

    def test_unlock_after_interval_fires(self):
        g = _greeter(min_interval_min=60)
        now = time.monotonic()
        g._last_fire = now
        assert g.should_fire(now + 3600) is True

    def test_zero_interval_always_fires(self):
        g = _greeter(min_interval_min=0)
        now = time.monotonic()
        g._last_fire = now
        assert g.should_fire(now + 0.1) is True


def _run_inline(coro, loop):
    """Execute the greeting coroutine synchronously for tests."""
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def inline_dispatch(monkeypatch):
    monkeypatch.setattr(sg.asyncio, "run_coroutine_threadsafe", _run_inline)
    yield


class TestUnlockTransition:
    def test_fires_on_lock_to_unlock(self, inline_dispatch):
        fired = []
        state = {"locked": True}

        async def cb():
            fired.append(1)

        g = UnlockGreeter(
            on_unlock=cb,
            min_interval_min=0,
            poll_s=0.05,
            debounce_s=0.0,
            locked_fn=lambda: state["locked"],
        )
        g.start(asyncio.new_event_loop())
        try:
            time.sleep(0.15)  # locked; no fire
            state["locked"] = False  # unlock
            time.sleep(0.3)
        finally:
            g.stop()
        assert len(fired) == 1

    def test_no_fire_without_unlock_edge(self, inline_dispatch):
        # Baseline is None: a PC that stays unlocked the whole watch produces
        # no edge, hence no greeting (and no greeting right after a restart).
        fired = []

        async def cb():
            fired.append(1)

        g = UnlockGreeter(
            on_unlock=cb,
            min_interval_min=0,
            poll_s=0.05,
            debounce_s=0.0,
            locked_fn=lambda: False,
        )
        g.start(asyncio.new_event_loop())
        try:
            time.sleep(0.25)
        finally:
            g.stop()
        assert fired == []

    def test_flicker_debounced(self, inline_dispatch):
        fired = []
        state = {"locked": True}

        async def cb():
            fired.append(1)

        g = UnlockGreeter(
            on_unlock=cb,
            min_interval_min=0,
            poll_s=0.05,
            debounce_s=0.2,
            locked_fn=lambda: state["locked"],
        )
        g.start(asyncio.new_event_loop())
        try:
            time.sleep(0.1)
            state["locked"] = False
            time.sleep(0.05)  # unlock...
            state["locked"] = True  # ...relock before debounce elapses
            time.sleep(0.4)
        finally:
            g.stop()
        assert fired == []


class _FakeToolbox:
    @staticmethod
    async def execute(name, args):
        return '{"total": 5}'


class _FakeScheduler:
    @staticmethod
    def list_tasks():
        return []


class _FakeOrch:
    toolbox = _FakeToolbox()
    scheduler = _FakeScheduler()


class TestGreetingRotation:
    def test_styles_rotate(self):
        orch = _FakeOrch()
        texts = [
            asyncio.new_event_loop().run_until_complete(build_greeting_text(orch, i))
            for i in range(4)
        ]
        # 4 styles = 4 different lines
        assert len(set(texts)) == 4
        assert "unread email" in texts[1]  # mail style with fake 5 unread

    def test_mail_style_falls_back_without_data(self):
        class EmptyToolbox:
            @staticmethod
            async def execute(name, args):
                raise RuntimeError("offline")

        class Orch:
            toolbox = EmptyToolbox()
            scheduler = _FakeScheduler()

        text = asyncio.new_event_loop().run_until_complete(build_greeting_text(Orch(), 1))
        assert "unread" not in text  # fell back to welcome
        assert text.count("sir") <= 1  # "sir" at most once

    def test_build_style_varies(self):
        orch = _FakeOrch()
        a = asyncio.new_event_loop().run_until_complete(build_greeting_text(orch, 3))
        b = asyncio.new_event_loop().run_until_complete(build_greeting_text(orch, 7))
        assert a != b  # different build lines on successive build slots
