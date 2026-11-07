"""Wave 7 failure-path tests for core.prompt_opt.

Contract: corrupt persisted state (prompts.json / examples.jsonl) is loud,
never silently reset; unparseable model output reports [] with a diagnostic;
render_prompt returns an error string for missing variables.
"""

from __future__ import annotations

from core.config import Config
from core.prompt_opt import PromptOptimizer


def make_cfg(tmp_path) -> Config:
    cfg = Config.__new__(Config)
    cfg.db_path = tmp_path / "state.db"
    return cfg


def make_opt(tmp_path) -> PromptOptimizer:
    return PromptOptimizer(router=None, client=None, config=make_cfg(tmp_path))


def test_corrupt_prompts_file_is_loud(tmp_path, capsys):
    (tmp_path / "prompts.json").write_text("{not json", encoding="utf-8")
    opt = make_opt(tmp_path)
    assert opt.prompts == {}
    out = capsys.readouterr().out
    assert "corrupt store prompts.json" in out
    # P0: quarantined aside (recoverable), not left corrupt
    assert list(tmp_path.glob("prompts.json.corrupt-*"))
    assert "quarantined" in out


def test_corrupt_examples_file_is_loud(tmp_path, capsys):
    (tmp_path / "examples.jsonl").write_text("{not json\n", encoding="utf-8")
    opt = make_opt(tmp_path)
    assert opt.examples == {}
    # P0: JSONL skips malformed lines individually (loud count), keeps the rest
    assert "malformed example lines" in capsys.readouterr().out


def test_examples_keep_good_lines_despite_bad(tmp_path, capsys):
    import json as _json

    good = {"id": "e1", "input": "a", "output": "b", "quality_score": 0.9}
    (tmp_path / "examples.jsonl").write_text(_json.dumps(good) + "\n{bad line\n", encoding="utf-8")
    opt = make_opt(tmp_path)
    assert set(opt.examples) == {"e1"}
    assert "malformed example lines" in capsys.readouterr().out


def test_missing_state_files_are_quiet(tmp_path, capsys):
    opt = make_opt(tmp_path)
    assert opt.prompts == {} and opt.examples == {}
    assert capsys.readouterr().out == ""


def test_render_prompt_missing_variable_returns_error_string(tmp_path):
    opt = make_opt(tmp_path)
    pid = opt.register_prompt("greet", "Hello {name}, you owe {amount}", ["name", "amount"])
    result = opt.render_prompt(pid, name="sir")  # missing 'amount'
    assert "missing variable" in result and "amount" in result


def test_render_prompt_unknown_id_returns_none(tmp_path):
    opt = make_opt(tmp_path)
    assert opt.render_prompt("nope") is None


async def test_bootstrap_examples_unparseable_model_output_is_loud(tmp_path, capsys):
    class GarbageRouter:
        async def chat(self, messages, tools, task_type):
            yield "fast", "this is definitely not JSON"

    cfg = make_cfg(tmp_path)
    opt = PromptOptimizer(router=GarbageRouter(), client=None, config=cfg)
    result = await opt.bootstrap_examples("summarize mail", count=3)
    assert result == []
    assert "unparseable output" in capsys.readouterr().out
