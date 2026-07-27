"""Safe arithmetic evaluator for LLM-authored meter formulas.

An LLM writes `rate_formula` and the engine runs it every tick. That makes the
formula UNTRUSTED INPUT executing on the host, so `eval()` is out — it would be
remote code execution in your own house, reachable by anything that can talk a
model into emitting `__import__('os').system(...)`.

Instead we parse with `ast` and walk an explicit whitelist: numbers, names bound
to the supplied variables, + - * / // % **, unary +/-, comparisons, and a short
list of pure math functions. Anything else — attribute access, subscripts, calls
to unlisted names, lambdas, comprehensions — raises FormulaError. Whitelist, not
blacklist: unknown node types are refused by default rather than allowed by
oversight.

Nothing here can import, assign, loop, or reach an object's attributes, so the
worst a hostile formula achieves is a FormulaError or a silly number.
"""

from __future__ import annotations

import ast
import math

__all__ = ["FormulaError", "evaluate", "validate"]


class FormulaError(ValueError):
    """Formula was unparseable, used a forbidden construct, or blew up at runtime."""


# Pure, total-ish, no I/O, no object access.
_FUNCTIONS = {
    "min": min, "max": max, "abs": abs, "round": round,
    "floor": math.floor, "ceil": math.ceil, "sqrt": math.sqrt,
    "exp": math.exp, "log": math.log, "pow": pow,
    "sin": math.sin, "cos": math.cos,
    # Clamp shows up in almost every rate formula; giving the model a primitive
    # beats watching it hand-roll nested min/max and get the order wrong.
    "clamp": lambda v, lo, hi: lo if v < lo else (hi if v > hi else v),
}

_CONSTANTS = {"pi": math.pi, "e": math.e, "True": True, "False": False}

_BINOPS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}

_COMPARES = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}

# A formula is a rate, not a program. This caps pathological input (deep nesting,
# 10k-character expressions) before it reaches the parser.
_MAX_LEN = 500
_MAX_DEPTH = 25
# ** with large operands is the one whitelisted op that can hang the process
# (2**10**9). Exponents are capped rather than the operation removed, since
# decay curves legitimately want them.
_MAX_EXPONENT = 64


def _depth(node: ast.AST, current: int = 0) -> int:
    if current > _MAX_DEPTH:
        raise FormulaError(f"formula nests deeper than {_MAX_DEPTH} levels")
    children = list(ast.iter_child_nodes(node))
    if not children:
        return current
    return max(_depth(c, current + 1) for c in children)


def _eval_node(node: ast.AST, variables: dict[str, float]):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, variables)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, bool)):
            return node.value
        raise FormulaError(f"only numeric literals allowed, got {node.value!r}")

    if isinstance(node, ast.Name):
        if node.id in variables:
            return variables[node.id]
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise FormulaError(
            f"unknown name {node.id!r} "
            f"(available: {sorted(variables) + sorted(_CONSTANTS)})")

    if isinstance(node, ast.BinOp):
        op = _BINOPS.get(type(node.op))
        if op is None:
            raise FormulaError(f"operator {type(node.op).__name__} not allowed")
        left, right = _eval_node(node.left, variables), _eval_node(node.right, variables)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_EXPONENT:
            raise FormulaError(f"exponent {right} exceeds cap {_MAX_EXPONENT}")
        try:
            return op(left, right)
        except ZeroDivisionError:
            raise FormulaError("division by zero") from None

    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            return -_eval_node(node.operand, variables)
        if isinstance(node.op, ast.UAdd):
            return +_eval_node(node.operand, variables)
        if isinstance(node.op, ast.Not):
            return not _eval_node(node.operand, variables)
        raise FormulaError(f"unary {type(node.op).__name__} not allowed")

    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, variables)
        for op, comparator in zip(node.ops, node.comparators):
            fn = _COMPARES.get(type(op))
            if fn is None:
                raise FormulaError(f"comparison {type(op).__name__} not allowed")
            right = _eval_node(comparator, variables)
            if not fn(left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.BoolOp):
        values = [_eval_node(v, variables) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)

    # Ternary — `rate if condition else other` is the natural way to express a
    # threshold, so it is worth supporting explicitly.
    if isinstance(node, ast.IfExp):
        return (_eval_node(node.body, variables) if _eval_node(node.test, variables)
                else _eval_node(node.orelse, variables))

    if isinstance(node, ast.Call):
        # Only bare `name(...)` — never `obj.method(...)`, which is the door to
        # attribute traversal and thus to everything.
        if not isinstance(node.func, ast.Name):
            raise FormulaError("only direct calls to whitelisted functions allowed")
        fn = _FUNCTIONS.get(node.func.id)
        if fn is None:
            raise FormulaError(
                f"unknown function {node.func.id!r} "
                f"(available: {sorted(_FUNCTIONS)})")
        if node.keywords:
            raise FormulaError("keyword arguments not allowed")
        args = [_eval_node(a, variables) for a in node.args]
        try:
            return fn(*args)
        except (TypeError, ValueError) as e:
            raise FormulaError(f"{node.func.id}(): {e}") from None

    raise FormulaError(f"{type(node).__name__} is not allowed in a formula")


def evaluate(expression: str, variables: dict[str, float] | None = None) -> float:
    """Evaluate `expression` against `variables`. Raises FormulaError on anything
    unparseable, non-whitelisted, or non-finite."""
    variables = variables or {}
    if not isinstance(expression, str):
        raise FormulaError(f"formula must be a string, got {type(expression).__name__}")
    expression = expression.strip()
    if not expression:
        return 0.0
    if len(expression) > _MAX_LEN:
        raise FormulaError(f"formula exceeds {_MAX_LEN} characters")

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"syntax error: {e.msg}") from None

    _depth(tree)
    result = _eval_node(tree, variables)

    try:
        result = float(result)
    except (TypeError, ValueError):
        raise FormulaError(f"formula produced non-numeric {result!r}") from None
    # NaN/inf would silently poison every downstream meter and never recover.
    if not math.isfinite(result):
        raise FormulaError(f"formula produced non-finite {result}")
    return result


def validate(expression: str, variables: dict[str, float] | None = None) -> str | None:
    """Dry-run a formula. Returns None if usable, else the error message.

    Called at the boundary where an LLM proposes a formula, so a bad one is
    rejected at authoring time — with the error fed back for a repair attempt —
    rather than exploding mid-tick three turns later.
    """
    try:
        evaluate(expression, variables)
        return None
    except FormulaError as e:
        return str(e)
