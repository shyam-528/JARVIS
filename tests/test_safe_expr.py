"""Tests for the safe gate-expression evaluator and the shell blocklist."""

from __future__ import annotations

import pytest

from core.code_agent import ShellExecutor
from core.safe_expr import eval_gate


@pytest.mark.parametrize(
    "expr,variables,expected",
    [
        ("", {}, True),
        ("x > 3", {"x": 5}, True),
        ("x > 3", {"x": 2}, False),
        ("a and b", {"a": 1, "b": True}, True),
        ("a or b", {"a": 0, "b": 0}, False),
        ("not a", {"a": True}, False),
        ("len(items) >= 2", {"items": [1, 2]}, True),
        ("x in items", {"x": 1, "items": [1, 2]}, True),
        ("x not in items", {"x": 9, "items": [1, 2]}, True),
        ('"ok" if x else "no"', {"x": 0}, True),  # falsy x -> "no"; bool("no") is True
        ('status == "done"', {"status": "done"}, True),
        ("items[0] == 1", {"items": [1]}, True),
        ("d['k'] == 2", {"d": {"k": 2}}, True),
        # fail-closed cases
        ("9**9**9", {}, False),  # DoS bomb blocked
        ("1000000**999", {}, False),  # exponent too large
        ("x.__class__", {"x": 1}, False),  # attribute access blocked
        ("import os", {}, False),
        ("[i for i in range(5)]", {}, False),  # comprehension blocked
        ("unknown_var > 1", {}, False),  # unknown name fails closed
        ("x / 0", {"x": 1}, False),  # zero division fails closed
        ("open('x')", {}, False),  # arbitrary call blocked
        ("(lambda: 1)()", {}, False),
        ("x" * 2000, {}, False),  # expression too long
    ],
)
def test_eval_gate(expr, variables, expected):
    assert eval_gate(expr, variables) is expected


@pytest.mark.parametrize(
    "command,expected_safe",
    [
        # destructive commands: hard-blocked
        ("Remove-Item -Recurse -Force C:\\", False),
        ("Remove-Item -Recurse -Force E:\\", False),
        ('Remove-Item -Recurse -Force "C:\\"', False),
        ("Remove-Item -Recurse -Force C:\\Windows", False),
        ("Remove-Item -Recurse -Force %USERPROFILE%", False),
        ("Remove-Item -Force -Recurse C:\\", False),  # flags in either order
        ("Format-Volume -DriveLetter C", False),
        ("Clear-Disk -Number 0", False),
        ("Remove-Partition -DiskNumber 0", False),
        ("del /f important.txt", False),
        ("del /s /q somefolder", False),
        ("erase /s /q sub", False),
        ("vssadmin delete shadows /all", False),
        ("cipher /w:C:\\", False),
        # legitimate commands: still permitted (via approval flow or readonly)
        ("Remove-Item -Recurse -Force D:\\data", True),  # subdir delete -> approval
        ("Remove-Item -Recurse build", True),
        ("del important.txt", True),
        ("echo hello", True),
        ("Get-ScheduledTask", True),
    ],
)
def test_shell_blocklist(command, expected_safe):
    safe, _ = ShellExecutor().is_safe(command)
    assert safe is expected_safe
