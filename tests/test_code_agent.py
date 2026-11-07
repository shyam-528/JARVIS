from __future__ import annotations

import pytest

from core.code_agent import ApprovalQueue, CodeAgent, CodeEditor, ShellExecutor


@pytest.fixture
def agent():
    return CodeAgent(auto_approve=False)


@pytest.fixture
def auto_agent():
    return CodeAgent(auto_approve=True)


def test_read_file(tmp_path):
    f = tmp_path / "test.py"
    f.write_text("line1\nline2\nline3")
    editor = CodeEditor()
    result = editor.read_file(str(f))
    assert "error" not in result
    assert result["total_lines"] == 3
    assert "line1" in result["content"]


def test_read_file_not_found():
    result = CodeEditor().read_file("nonexistent_file.txt")
    assert "error" in result


def test_read_file_line_range(tmp_path):
    f = tmp_path / "test.py"
    f.write_text("\n".join(f"line{i}" for i in range(1, 11)))
    result = CodeEditor().read_file(str(f), start_line=3, end_line=5)
    assert "line3" in result["content"]
    assert "line4" in result["content"]
    assert "line9" not in result["content"]


def test_write_file_generates_diff(tmp_path):
    f = tmp_path / "new.py"
    editor = CodeEditor()
    result = editor.write_file(str(f), "hello world")
    assert result["existed"] is False
    assert result["lines"] == 1
    assert "hello world" in result["diff"]


def test_apply_write(tmp_path):
    f = tmp_path / "test.txt"
    assert CodeEditor().apply_write(str(f), "content here")
    assert f.read_text() == "content here"


def test_edit_lines(tmp_path):
    f = tmp_path / "test.py"
    f.write_text("def foo():\n    pass\n\ndef bar():\n    pass")
    result = CodeEditor().edit_lines(
        str(f), [{"line": 1, "old": "def foo", "new": "def foo_updated"}]
    )
    assert "applied" in result
    assert result["applied"] == 1
    assert "foo_updated" in result["new_content"]


def test_shell_executor_blocks_dangerous():
    shell = ShellExecutor()
    safe, _ = shell.is_safe("rm -rf /")
    assert not safe
    safe, _ = shell.is_safe("echo hello")
    assert safe


def test_shell_execute_echo():
    shell = ShellExecutor(timeout_s=10)
    result = shell.execute("echo hello_world")
    assert result.get("exit_code") == 0
    assert "hello_world" in result.get("stdout", "")


def test_approval_queue_submit_and_approve():
    queue = ApprovalQueue(auto_approve=False)
    change = queue.submit("write_file", "/tmp/test.py", "test change", diff="-old\n+new")
    assert queue.has_pending()
    approved = queue.approve(change.change_id)
    assert approved is not None
    assert approved.status == "approved"
    assert not queue.has_pending()


def test_approval_queue_reject():
    queue = ApprovalQueue(auto_approve=False)
    change = queue.submit("write_file", "/tmp/test.py", "test")
    rejected = queue.reject(change.change_id)
    assert rejected.status == "rejected"
    assert not queue.has_pending()


def test_approval_auto_approve():
    queue = ApprovalQueue(auto_approve=True)
    change = queue.submit("write_file", "/tmp/test.py", "test")
    assert change.status == "approved"
    assert not queue.has_pending()


def test_agent_write_requires_approval(agent, tmp_path):
    f = tmp_path / "new.txt"
    result = agent.request_write(str(f), "hello")
    assert result["pending"] is True
    assert agent.queue.has_pending()
    assert not f.exists()


def test_agent_auto_approve_writes(auto_agent, tmp_path):
    f = tmp_path / "new.txt"
    result = auto_agent.request_write(str(f), "hello")
    assert result["pending"] is False
    assert f.exists()
    assert f.read_text() == "hello"


def test_agent_approve_and_apply(agent, tmp_path):
    f = tmp_path / "test.txt"
    result = agent.request_write(str(f), "test content")
    assert result["pending"] is True
    approve_result = agent.approve(result["change_id"])
    assert approve_result.get("applied") is True
    assert f.exists()


def test_agent_reject(agent, tmp_path):
    f = tmp_path / "test.txt"
    result = agent.request_write(str(f), "test")
    reject_result = agent.reject(result["change_id"])
    assert reject_result["rejected"] is True
    assert not f.exists()


def test_agent_search_code(tmp_path, agent):
    (tmp_path / "a.py").write_text("def hello():\n    print('world')\n")
    (tmp_path / "b.py").write_text("def goodbye():\n    pass")
    result = agent.editor.search_code(str(tmp_path), "def hello")
    assert result["total"] >= 1
    assert any("hello" in m["text"] for m in result["matches"])


def test_readonly_commands_need_no_approval(agent):
    shell = agent.shell
    assert shell.is_readonly("schtasks /query /fo LIST /v")
    assert shell.is_readonly('SCHTASKS /Query /TN "JARVIS MARK 3"')
    assert shell.is_readonly("Get-ScheduledTask")
    assert shell.is_readonly("git status")
    assert shell.is_readonly("git log --oneline -5")
    # mutating or chained commands still need approval
    assert not shell.is_readonly("schtasks /create /tn evil /tr calc")
    assert not shell.is_readonly("schtasks /query & del foo")
    assert not shell.is_readonly("git status; rm -rf /tmp/x")
    assert not shell.is_readonly("del important.txt")


def test_readonly_pipe_filters_need_no_approval(agent):
    shell = agent.shell
    assert shell.is_readonly('schtasks /query /fo LIST /v | findstr /i "jarvis"')
    assert shell.is_readonly(
        'schtasks /query /fo LIST /v | findstr /i "jarvis\\|upgrade\\|reminder"'
    )
    assert shell.is_readonly("git log --oneline -5 | more")
    # anything beyond a pure filter falls back to approval
    assert not shell.is_readonly("schtasks /query | findstr x & del foo")
    assert not shell.is_readonly("schtasks /query | findstr x > out.txt")
    assert not shell.is_readonly("git status | sort.exe")
    assert not shell.is_readonly("del a.txt | findstr x")


def test_readonly_command_runs_immediately(agent):
    result = agent.request_command("git status")
    assert result["pending"] is False
    assert result.get("readonly") is True
    assert "output" in result
    assert not agent.queue.has_pending()
