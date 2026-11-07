"""Regression tests for skills/nl_shortcuts.py (extracted from jarvis.py).

Covers the deterministic fast paths: routines, mail, and the WhatsApp
pending-send completion. Uses a fake orchestrator/toolbox so no LLM,
network, or config is touched.
"""

import asyncio
import json

from skills.nl_shortcuts import (
    try_mail_shortcut,
    try_pc_shortcut,
    try_routine_shortcut,
)


class FakeToolbox:
    def __init__(self):
        self.calls = []

    async def execute(self, tool, args_json):
        self.calls.append((tool, json.loads(args_json or "{}")))
        return f"ok:{tool}"


class FakeOrch:
    def __init__(self, history=None):
        self.toolbox = FakeToolbox()
        self.history = history or []


class FakeConsole:
    def __init__(self):
        self.printed = []

    def print(self, *args, **kwargs):
        self.printed.append(str(args[0]) if args else "")


def test_routine_starting_work_runs_morning_bundle():
    orch, con = FakeOrch(), FakeConsole()
    hit = asyncio.run(try_routine_shortcut(orch, "starting work", con))
    assert hit is True
    assert orch.toolbox.calls == [("morning_start", {})]
    assert con.printed and "ok:morning_start" in con.printed[0]


def test_routine_unrelated_input_falls_through():
    orch, con = FakeOrch(), FakeConsole()
    hit = asyncio.run(try_routine_shortcut(orch, "tell me about recursion", con))
    assert hit is False
    assert orch.toolbox.calls == []


def test_mail_unread_question_uses_mail_unread_tool():
    orch, con = FakeOrch(), FakeConsole()
    hit = asyncio.run(try_mail_shortcut(orch, "any unread mail?", con))
    assert hit is True
    assert ("mail_unread", {"limit": 8}) in orch.toolbox.calls


def test_mail_bare_approval_sends_draft():
    orch, con = FakeOrch(), FakeConsole()
    hit = asyncio.run(try_mail_shortcut(orch, "send it", con))
    assert hit is True
    assert ("mail_send_draft", {}) in orch.toolbox.calls


def test_mail_unrelated_input_falls_through():
    orch, con = FakeOrch(), FakeConsole()
    hit = asyncio.run(try_mail_shortcut(orch, "what is the capital of France?", con))
    assert hit is False
    assert orch.toolbox.calls == []


def test_mail_channel_only_followup_completes_pending_send():
    history = [{"role": "user", "content": "send hello there to +12345678901"}]
    orch, con = FakeOrch(history), FakeConsole()
    hit = asyncio.run(try_mail_shortcut(orch, "in whatsapp", con))
    assert hit is True
    assert ("wa_send", {"chat": "+12345678901", "text": "hello there"}) in orch.toolbox.calls


def test_pc_unmatched_input_falls_through():
    orch, con = FakeOrch(), FakeConsole()
    hit = asyncio.run(try_pc_shortcut(orch, "explain quantum entanglement", con))
    assert hit is False
    assert orch.toolbox.calls == []
