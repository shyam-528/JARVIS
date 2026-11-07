"""Safe expression evaluation for workflow/pipeline gate conditions.

Replaces ``eval(expr, {"__builtins__": {}}, vars)`` with an AST whitelist
evaluator. Even sandboxed ``eval`` allowed denial-of-service via huge
computations (e.g. ``9**9**9``) and any expression an LLM can dream up.
This evaluator only permits constants, names from the caller-provided
variable mapping, boolean/comparison/membership ops, arithmetic (with an
exponent cap), indexing, ternaries, and len/str/int/float/bool calls.
Anything else raises SafeExprError and the gate fails closed (False).
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Mapping
from typing import Any

__all__ = ["SafeExprError", "eval_gate"]


class SafeExprError(ValueError):
    """Raised when an expression uses a disallowed construct."""


# Hard resource limits: bound evaluation cost regardless of input.
MAX_NODES = 200  # total AST nodes allowed in one expression
MAX_EXPONENT = 1000  # cap for the right-hand side of `**`
MAX_INT = 10**12  # results larger than this are rejected
MAX_EXPR_LEN = 1000  # characters

_ALLOWED_FUNCS = {"len": len, "str": str, "int": int, "float": float, "bool": bool}

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}

_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}


def _check_result(value: Any) -> Any:
    """Reject absurd intermediate results (memory/CPU bombs)."""
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > MAX_INT:
        raise SafeExprError(f"integer result exceeds limit ({MAX_INT})")
    if isinstance(value, (str, list, tuple, dict)) and len(value) > 100_000:
        raise SafeExprError("container result exceeds size limit")
    return value


def _eval(node: ast.AST, variables: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body, variables)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in variables:
            return variables[node.id]
        raise SafeExprError(f"unknown name: {node.id!r}")
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in node.values:
                result = _eval(v, variables)
                if not result:
                    return result
            return result
        if isinstance(node.op, ast.Or):
            result = False
            for v in node.values:
                result = _eval(v, variables)
                if result:
                    return result
            return result
        raise SafeExprError(f"disallowed boolean op: {type(node.op).__name__}")
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise SafeExprError(f"disallowed unary op: {type(node.op).__name__}")
        return _check_result(op(_eval(node.operand, variables)))
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise SafeExprError(f"disallowed binary op: {type(node.op).__name__}")
        left = _eval(node.left, variables)
        right = _eval(node.right, variables)
        if isinstance(node.op, ast.Pow) and (
            not isinstance(right, (int, float)) or abs(right) > MAX_EXPONENT
        ):
            raise SafeExprError(f"exponent too large (max {MAX_EXPONENT})")
        return _check_result(op(left, right))
    if isinstance(node, ast.Compare):
        left = _eval(node.left, variables)
        for op_node, comparator in zip(node.ops, node.comparators, strict=True):
            op = _CMP_OPS.get(type(op_node))
            if op is None:
                raise SafeExprError(f"disallowed comparison: {type(op_node).__name__}")
            right = _eval(comparator, variables)
            if not op(left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _eval(node.body if _eval(node.test, variables) else node.orelse, variables)
    if isinstance(node, ast.Subscript):
        value = _eval(node.value, variables)
        key = _eval(node.slice, variables)
        try:
            return value[key]
        except (KeyError, IndexError, TypeError) as e:
            raise SafeExprError(f"bad subscript: {e}") from e
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            raise SafeExprError("only len/str/int/float/bool calls are allowed")
        if node.keywords:
            raise SafeExprError("keyword arguments are not allowed")
        args = [_eval(a, variables) for a in node.args]
        try:
            return _check_result(_ALLOWED_FUNCS[node.func.id](*args))
        except (TypeError, ValueError) as e:
            raise SafeExprError(f"bad call to {node.func.id}: {e}") from e
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return _check_result([_eval(e, variables) for e in node.elts])
    if isinstance(node, ast.Dict):
        return _check_result(
            {
                _eval(k, variables): _eval(v, variables)
                for k, v in zip(node.keys, node.values, strict=True)
                if k is not None
            }
        )
    raise SafeExprError(f"disallowed expression element: {type(node).__name__}")


def eval_gate(expr: str, variables: Mapping[str, Any]) -> bool:
    """Evaluate a gate/condition expression to a boolean.

    Empty/blank expressions are True (no gate). Any error — syntax,
    unknown name, disallowed construct, resource limit — yields False,
    matching the previous eval()-based fail-closed behavior.
    """
    if not expr or not expr.strip():
        return True
    if len(expr) > MAX_EXPR_LEN:
        return False
    try:
        tree = ast.parse(expr, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
            return False
        return bool(_eval(tree, variables))
    except (SafeExprError, SyntaxError, MemoryError, RecursionError, ZeroDivisionError):
        return False
