"""Safe `script` step support for the native and Playwright tester runners.

Customer-supplied script steps are a real foot-gun: a naive `eval` would let
a stray spec exfiltrate environment variables or shell out from inside
Retrace.  This module gates expressions through an AST whitelist so the only
operations that survive are deterministic data manipulation and assertions
against a frozen scope.

Allowed surface:
- literals, names (resolved against scope/helpers), comparisons, boolean and
  arithmetic operators, ternary, container literals, attribute access (no
  dunders), subscript, calls to a fixed helper allowlist, f-strings.
- helpers default to `len`, `int`, `str`, `bool`, `now_iso`, `uuid_str`,
  `random_token`, `lower`, `upper`, `contains`, `format_template`.

Disallowed surface:
- imports, lambdas, comprehensions, generators, yield/await, starred unpack,
  function/class definitions, attributes that start with `_` (so `__class__`
  walks back to `os` are blocked), names that start with `__`.
"""

from __future__ import annotations

import ast
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping


class ScriptError(ValueError):
    """Raised when a script expression violates the sandbox or fails to run."""


_ALLOWED_NODE_TYPES: frozenset[type[ast.AST]] = frozenset(
    {
        ast.Expression,
        ast.Constant,
        ast.Name,
        ast.Attribute,
        ast.Subscript,
        ast.Compare,
        ast.BoolOp,
        ast.UnaryOp,
        ast.BinOp,
        ast.Call,
        ast.IfExp,
        ast.List,
        ast.Tuple,
        ast.Dict,
        ast.Set,
        ast.Load,
        ast.Slice,
        ast.JoinedStr,
        ast.FormattedValue,
        ast.keyword,
        # Operators are AST nodes too, not just markers.
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.In,
        ast.NotIn,
        ast.Is,
        ast.IsNot,
        ast.And,
        ast.Or,
        ast.Not,
        ast.USub,
        ast.UAdd,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.Pow,
    }
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid_str() -> str:
    return str(uuid.uuid4())


def _random_token(length: int = 16) -> str:
    """Return a hex token of the requested length (2..64 chars)."""
    n = max(2, min(int(length), 64))
    # token_hex returns 2*n chars; cap and slice.
    return secrets.token_hex(32)[:n]


def _contains(haystack: Any, needle: Any) -> bool:
    if haystack is None:
        return False
    try:
        return needle in haystack
    except TypeError:
        return False


def _format_template(template: str, **values: Any) -> str:
    try:
        return str(template).format(**values)
    except (KeyError, IndexError, ValueError) as exc:
        raise ScriptError(f"format_template error: {exc}") from exc


DEFAULT_SCRIPT_HELPERS: Mapping[str, Callable[..., Any]] = {
    "len": len,
    "int": int,
    "str": str,
    "bool": bool,
    "lower": lambda v: str(v).lower(),
    "upper": lambda v: str(v).upper(),
    "now_iso": _now_iso,
    "uuid_str": _uuid_str,
    "random_token": _random_token,
    "contains": _contains,
    "format_template": _format_template,
}


@dataclass
class ScriptStepResult:
    set_vars: dict[str, Any] = field(default_factory=dict)
    assertions: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and all(a.get("ok") for a in self.assertions)


def safe_eval(
    expression: str,
    *,
    scope: Mapping[str, Any],
    helpers: Mapping[str, Callable[..., Any]] = DEFAULT_SCRIPT_HELPERS,
) -> Any:
    """Evaluate `expression` against `scope` and `helpers` under the AST whitelist."""
    if not isinstance(expression, str):
        raise ScriptError("script expressions must be strings")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise ScriptError(f"syntax error: {exc.msg}") from exc
    _verify(tree)
    # Wrap native runtime errors (ZeroDivisionError, TypeError on `in` against
    # a non-iterable, etc.) in ScriptError so callers - run_script_step in
    # particular - can rely on a single exception type for failed assertions
    # rather than letting the run abort.
    try:
        return _eval(tree.body, scope, helpers)
    except ScriptError:
        raise
    except Exception as exc:
        raise ScriptError(f"evaluation error: {exc}") from exc


def render_template(template: str, scope: Mapping[str, Any]) -> str:
    """Substitute `{{ name }}` patterns in `template` with values from `scope`.

    Only top-level keys in `scope` (or `scope['vars']`) are recognised.  Any
    unresolved or non-stringable value renders as the empty string so a
    misspelled variable does not stamp `None` into URLs.
    """
    if "{{" not in template:
        return template

    out: list[str] = []
    i = 0
    n = len(template)
    while i < n:
        start = template.find("{{", i)
        if start == -1:
            out.append(template[i:])
            break
        out.append(template[i:start])
        end = template.find("}}", start + 2)
        if end == -1:
            out.append(template[start:])
            break
        name = template[start + 2 : end].strip()
        out.append(_lookup_template(name, scope))
        i = end + 2
    return "".join(out)


def _lookup_template(name: str, scope: Mapping[str, Any]) -> str:
    if not name or any(ch in name for ch in "()[]{}!@#$%^&*+=,;\\\""):
        return ""
    parts = [p.strip() for p in name.split(".") if p.strip()]
    if not parts:
        return ""
    cursor: Any = scope
    for part in parts:
        if part.startswith("_"):
            return ""
        if isinstance(cursor, Mapping):
            cursor = cursor.get(part)
        else:
            cursor = getattr(cursor, part, None)
        if cursor is None:
            return ""
    if isinstance(cursor, (dict, list, tuple, set)):
        return ""
    return str(cursor)


def run_script_step(
    step: Mapping[str, Any],
    *,
    scope: dict[str, Any],
    helpers: Mapping[str, Callable[..., Any]] = DEFAULT_SCRIPT_HELPERS,
) -> ScriptStepResult:
    """Execute the `set`/`assert` blocks of a single `script` step.

    `scope` is mutated in place: `set` assignments are merged into
    `scope['vars']`.  Assertions never raise — they're surfaced as result
    rows so the run can continue and the run summary reflects what failed.
    """
    result = ScriptStepResult()
    raw_set = step.get("set") or {}
    raw_assert = step.get("assert") or []
    if not isinstance(raw_set, Mapping):
        result.error = "script.set must be a mapping"
        return result
    if not isinstance(raw_assert, (list, tuple)):
        result.error = "script.assert must be a list of expressions"
        return result

    vars_dict = scope.setdefault("vars", {})
    if not isinstance(vars_dict, dict):
        # Treat a non-dict 'vars' as misconfiguration rather than silently
        # overwriting - the scope is shared across steps, so we'd be
        # destroying prior state.
        result.error = "scope['vars'] must be a dict"
        return result

    for name, expr in raw_set.items():
        if not isinstance(name, str) or not name.isidentifier() or name.startswith("_"):
            result.error = f"invalid variable name: {name!r}"
            return result
        try:
            value = safe_eval(str(expr), scope=scope, helpers=helpers)
        except ScriptError as exc:
            result.error = f"set {name}: {exc}"
            return result
        if isinstance(value, (dict, list)):
            # Defensive copy so subsequent steps mutating the scope don't
            # bleed back into the source helper's internal state.
            value = _deep_copy_safe(value)
        result.set_vars[name] = value
        vars_dict[name] = value

    for raw in raw_assert:
        record: dict[str, Any] = {
            "id": "script-assert",
            "type": "script",
            "expression": str(raw),
        }
        try:
            ok = bool(safe_eval(str(raw), scope=scope, helpers=helpers))
            record["ok"] = ok
            record["message"] = "passed" if ok else "evaluated to false"
        except ScriptError as exc:
            record["ok"] = False
            record["message"] = str(exc)
        result.assertions.append(record)
    return result


def _deep_copy_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _deep_copy_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_copy_safe(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_deep_copy_safe(v) for v in value)
    return value


def _verify(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        node_type = type(node)
        if node_type not in _ALLOWED_NODE_TYPES:
            raise ScriptError(f"forbidden expression node: {node_type.__name__}")
        if isinstance(node, ast.Name):
            if node.id.startswith("__"):
                raise ScriptError(f"forbidden name: {node.id}")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise ScriptError(f"forbidden attribute: {node.attr}")
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ScriptError("calls must target a named helper, not an attribute")
            for kw in node.keywords:
                if kw.arg is None:
                    raise ScriptError("** unpacking is not allowed in script calls")
            for arg in node.args:
                if isinstance(arg, ast.Starred):
                    raise ScriptError("* unpacking is not allowed in script calls")
        elif isinstance(node, ast.Dict):
            # ast.Dict represents `{**other}` as a key=None entry; treat it
            # the same as ** unpacking on a call so the policy is consistent.
            if any(k is None for k in node.keys):
                raise ScriptError("** unpacking is not allowed in dict literals")


def _eval(node: ast.AST, scope: Mapping[str, Any], helpers: Mapping[str, Callable[..., Any]]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in scope:
            return scope[node.id]
        # Fall through to scope['vars'] so a `set` name can be referenced
        # bare in subsequent expressions ("len(token) > 0" rather than
        # "len(vars.token) > 0").  Top-level scope still wins on collision.
        nested_vars = scope.get("vars") if isinstance(scope, Mapping) else None
        if isinstance(nested_vars, Mapping) and node.id in nested_vars:
            return nested_vars[node.id]
        if node.id in helpers:
            return helpers[node.id]
        raise ScriptError(f"unknown name: {node.id}")
    if isinstance(node, ast.Attribute):
        target = _eval(node.value, scope, helpers)
        if isinstance(target, Mapping):
            if node.attr in target:
                return target[node.attr]
            raise ScriptError(f"missing attribute: {node.attr}")
        # Attribute access on objects (e.g. an httpx.Response) — only fields
        # that don't start with `_` survived `_verify`, but call out missing
        # ones explicitly so the test author gets a real error.
        try:
            return getattr(target, node.attr)
        except AttributeError as exc:
            raise ScriptError(str(exc)) from exc
    if isinstance(node, ast.Subscript):
        target = _eval(node.value, scope, helpers)
        index = _eval_slice(node.slice, scope, helpers)
        try:
            return target[index]
        except (KeyError, IndexError, TypeError) as exc:
            raise ScriptError(f"subscript error: {exc}") from exc
    if isinstance(node, ast.Compare):
        left = _eval(node.left, scope, helpers)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval(comparator, scope, helpers)
            if not _apply_compare(op, left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        # Short-circuit and preserve Python's truthy-value semantics:
        # `a and b` returns the first falsy operand or the last value, and
        # `a or b` returns the first truthy operand or the last value.
        if isinstance(node.op, ast.And):
            current: Any = True
            for child in node.values:
                current = _eval(child, scope, helpers)
                if not current:
                    return current
            return current
        if isinstance(node.op, ast.Or):
            current = False
            for child in node.values:
                current = _eval(child, scope, helpers)
                if current:
                    return current
            return current
    if isinstance(node, ast.UnaryOp):
        operand = _eval(node.operand, scope, helpers)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
    if isinstance(node, ast.BinOp):
        left = _eval(node.left, scope, helpers)
        right = _eval(node.right, scope, helpers)
        return _apply_binop(node.op, left, right)
    if isinstance(node, ast.IfExp):
        if _eval(node.test, scope, helpers):
            return _eval(node.body, scope, helpers)
        return _eval(node.orelse, scope, helpers)
    if isinstance(node, ast.List):
        return [_eval(elt, scope, helpers) for elt in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval(elt, scope, helpers) for elt in node.elts)
    if isinstance(node, ast.Set):
        return {_eval(elt, scope, helpers) for elt in node.elts}
    if isinstance(node, ast.Dict):
        # _verify rejects key=None (** unpacking) up front, so reaching this
        # branch with a None key would mean a bug in _verify - fail loud.
        return {
            _eval(k, scope, helpers): _eval(v, scope, helpers)
            for k, v in zip(node.keys, node.values)
        }
    if isinstance(node, ast.Call):
        func = _eval(node.func, scope, helpers)
        if not callable(func):
            raise ScriptError(f"not callable: {ast.dump(node.func)}")
        args = [_eval(a, scope, helpers) for a in node.args]
        kwargs = {kw.arg: _eval(kw.value, scope, helpers) for kw in node.keywords}
        try:
            return func(*args, **kwargs)
        except ScriptError:
            raise
        except Exception as exc:
            raise ScriptError(f"{getattr(func, '__name__', 'call')} failed: {exc}") from exc
    if isinstance(node, ast.JoinedStr):
        return "".join(str(_eval(part, scope, helpers)) for part in node.values)
    if isinstance(node, ast.FormattedValue):
        value = _eval(node.value, scope, helpers)
        # ast.FormattedValue.format_spec is ast.JoinedStr | None - never a
        # plain Constant - so recurse through the same evaluator to assemble
        # the spec string (which may itself reference scope vars in f"{x:{w}}").
        format_spec_str = (
            _eval(node.format_spec, scope, helpers) if node.format_spec else ""
        )
        return format(value, format_spec_str)
    raise ScriptError(f"unhandled expression node: {type(node).__name__}")


def _eval_slice(node: ast.AST, scope: Mapping[str, Any], helpers: Mapping[str, Callable[..., Any]]) -> Any:
    if isinstance(node, ast.Slice):
        lower = _eval(node.lower, scope, helpers) if node.lower is not None else None
        upper = _eval(node.upper, scope, helpers) if node.upper is not None else None
        step = _eval(node.step, scope, helpers) if node.step is not None else None
        return slice(lower, upper, step)
    return _eval(node, scope, helpers)


def _apply_compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    if isinstance(op, ast.Lt):
        return left < right
    if isinstance(op, ast.LtE):
        return left <= right
    if isinstance(op, ast.Gt):
        return left > right
    if isinstance(op, ast.GtE):
        return left >= right
    if isinstance(op, ast.In):
        return left in right
    if isinstance(op, ast.NotIn):
        return left not in right
    if isinstance(op, ast.Is):
        return left is right
    if isinstance(op, ast.IsNot):
        return left is not right
    raise ScriptError(f"unsupported comparator: {type(op).__name__}")


def _apply_binop(op: ast.operator, left: Any, right: Any) -> Any:
    if isinstance(op, ast.Add):
        return left + right
    if isinstance(op, ast.Sub):
        return left - right
    if isinstance(op, ast.Mult):
        return left * right
    if isinstance(op, ast.Div):
        return left / right
    if isinstance(op, ast.FloorDiv):
        return left // right
    if isinstance(op, ast.Mod):
        return left % right
    if isinstance(op, ast.Pow):
        return left**right
    raise ScriptError(f"unsupported operator: {type(op).__name__}")
