"""Wave 3 tests — tools & agents integrity."""

import ast
import asyncio

import pytest

from core.tool_use_v2 import ToolUseV2
from core.tools import Tool, ToolBox


def _box_with_echo():
    box = ToolBox()

    async def echo(text: str = "") -> str:
        return f"echo:{text}"

    async def boom() -> str:
        raise RuntimeError("kaboom")

    box.register(
        Tool(
            name="echo",
            description="echo",
            parameters={"type": "object", "properties": {}},
            handler=echo,
        )
    )
    box.register(
        Tool(
            name="boom",
            description="raises",
            parameters={"type": "object", "properties": {}},
            handler=boom,
        )
    )
    return box


# ---------- stale provider names ----------


def test_no_zen_providers_in_agents():
    from core.agent_pool import DEFAULT_AGENTS

    for name, spec in DEFAULT_AGENTS.items():
        assert not any(p.startswith("zen-") for p in spec.providers), name


def test_no_zen_providers_in_ensemble():
    from core.ensemble import MoERouter

    for kind, chain in MoERouter.EXPERTS.items():
        assert chain, kind  # no empty expert chain
        assert not any(p.startswith("zen-") for p in chain), kind


def test_no_zen_tier_in_budget_guard():
    from core.budget_guard import tier_of

    assert tier_of("zen-mimo") == "other"  # zen tier removed after provider prune


# ---------- toolbox error contract ----------


def test_execute_unknown_tool_returns_error_string():
    box = _box_with_echo()
    out = asyncio.run(box.execute("nope", "{}"))
    assert out.startswith("error: unknown tool")


def test_execute_bad_json_returns_error_string():
    box = _box_with_echo()
    out = asyncio.run(box.execute("echo", "{not json"))
    assert out.startswith("error: bad arguments JSON")


def test_execute_handler_exception_returns_error_string():
    box = _box_with_echo()
    out = asyncio.run(box.execute("boom", "{}"))
    assert out.startswith("error: RuntimeError: kaboom")


# ---------- tool_use_v2 ----------


def test_parallel_marks_error_string_results_as_failures():
    tu = ToolUseV2(_box_with_echo())
    results = asyncio.run(
        tu.execute_parallel(
            [
                ("1", "echo", {"text": "hi"}),
                ("2", "boom", {}),
                ("3", "missing_tool", {}),
            ]
        )
    )
    assert results[0].success and results[0].result == "echo:hi"
    assert not results[1].success and "kaboom" in results[1].error
    assert not results[2].success and "unknown tool" in results[2].error


def test_parallel_history_capped():
    tu = ToolUseV2(_box_with_echo())
    tu.max_history = 5
    for _ in range(4):
        asyncio.run(
            tu.execute_parallel([("c", "echo", {"text": "x"}), ("c2", "echo", {"text": "y"})])
        )
    assert len(tu.execution_history) == 5


def test_tool_categories_have_no_dead_refs():
    tu = ToolUseV2(_box_with_echo())
    dead = {"fetch_url", "search_memory", "add_knowledge"}
    for cat, names in tu.tool_categories.items():
        assert not dead.intersection(names), cat


def test_import_chain_validates_required_keys():
    tu = ToolUseV2(_box_with_echo())
    with pytest.raises(ValueError):
        tu.import_chain({"id": "x"})  # missing name/steps
    with pytest.raises(ValueError):
        tu.import_chain({"id": "x", "name": "n", "steps": "not-a-list"})


def test_execute_chain_stops_on_failure_and_counts():
    tu = ToolUseV2(_box_with_echo())
    cid = tu.create_chain(
        "t",
        [
            {"tool": "boom", "args": {}, "output_var": "r1"},
            {"tool": "echo", "args": {"text": "{{r1}}"}, "output_var": "r2"},
        ],
    )
    results = asyncio.run(tu.execute_chain(cid))
    assert len(results) == 1  # second step never ran
    assert not results[0].success
    chain = tu.chains[cid]
    assert chain.failure_count == 1 and chain.success_count == 0


def test_execute_chain_variable_substitution():
    tu = ToolUseV2(_box_with_echo())
    cid = tu.create_chain(
        "t",
        [
            {"tool": "echo", "args": {"text": "hello"}, "output_var": "r1"},
            {"tool": "echo", "args": {"text": "{{r1}}!"}, "output_var": "r2"},
        ],
    )
    results = asyncio.run(tu.execute_chain(cid))
    assert results[1].result == "echo:echo:hello!"


# ---------- module contracts ----------


@pytest.mark.parametrize(
    "path",
    [
        "core/tools.py",
        "core/tool_use_v2.py",
        "core/agent_pool.py",
        "skills/fs.py",
        "skills/system.py",
        "skills/web.py",
        "skills/browser.py",
    ],
)
def test_module_has_contract_docstring(path):
    with open(path, encoding="utf-8") as f:
        doc = ast.get_docstring(ast.parse(f.read()))
    assert doc and doc.strip(), path
