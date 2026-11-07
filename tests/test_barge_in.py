"""Tests for the barge-in monitor decision logic (roadmap step 2a)."""

from __future__ import annotations

import time

from voice.mic import BargeInMonitor


def _mon(**kw):
    return BargeInMonitor(on_barge=lambda: None, should_stop=lambda: False, **kw)


class TestDecideLogic:
    def test_sustained_loud_speech_after_settle_triggers(self):
        m = _mon()
        assert m._decide(speech_run_ms=500, energy=900, floor=300, settled=True) is True

    def test_short_burst_rejected(self):
        m = _mon()
        assert m._decide(speech_run_ms=200, energy=900, floor=300, settled=True) is False

    def test_quiet_speech_rejected(self):
        m = _mon()
        # energy below margin*floor — speaker bleed, not the user
        assert m._decide(speech_run_ms=600, energy=400, floor=300, settled=True) is False

    def test_pre_settle_rejected(self):
        m = _mon()
        # speaker ramp-up window: never trigger, no matter how loud
        assert m._decide(speech_run_ms=900, energy=2000, floor=300, settled=False) is False

    def test_custom_trigger_ms(self):
        m = _mon(trigger_ms=800)
        assert m._decide(speech_run_ms=500, energy=900, floor=300, settled=True) is False
        assert m._decide(speech_run_ms=900, energy=900, floor=300, settled=True) is True

    def test_custom_energy_margin(self):
        m = _mon(energy_margin=3.0)
        assert m._decide(speech_run_ms=500, energy=800, floor=300, settled=True) is False
        assert m._decide(speech_run_ms=500, energy=1000, floor=300, settled=True) is True


class TestLifecycle:
    def test_start_stop_idempotent(self):
        fired = []
        m = BargeInMonitor(on_barge=lambda: fired.append(1), should_stop=lambda: True)
        m.start()
        m.start()  # second start is a no-op
        m.stop()
        m.stop()  # second stop is a no-op
        time.sleep(0.1)
        # should_stop() was True from the start -> thread exits without firing
        assert fired == []

    def test_triggered_flag_defaults_false(self):
        m = _mon()
        assert m.triggered is False
