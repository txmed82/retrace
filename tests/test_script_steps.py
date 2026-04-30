from __future__ import annotations

import pytest

from retrace.script_steps import (
    DEFAULT_SCRIPT_HELPERS,
    ScriptError,
    render_template,
    run_script_step,
    safe_eval,
)


# -- safe_eval: positive cases ------------------------------------------------


def test_safe_eval_evaluates_literals_and_arithmetic() -> None:
    assert safe_eval("1 + 2", scope={}) == 3
    assert safe_eval("'a' + 'b'", scope={}) == "ab"
    assert safe_eval("[1, 2, 3][1]", scope={}) == 2


def test_safe_eval_resolves_scope_variables() -> None:
    assert safe_eval("x + 1", scope={"x": 10}) == 11
    assert safe_eval(
        "vars.token == 'abc'",
        scope={"vars": {"token": "abc"}},
    ) is True


def test_safe_eval_calls_default_helpers() -> None:
    assert isinstance(safe_eval("uuid_str()", scope={}), str)
    assert safe_eval("len('hello')", scope={}) == 5
    assert safe_eval("contains('foobar', 'oo')", scope={}) is True


def test_safe_eval_supports_fstring_formatting() -> None:
    assert safe_eval("f'user-{n}'", scope={"n": 7}) == "user-7"


def test_safe_eval_honors_fstring_format_spec() -> None:
    # ast.FormattedValue.format_spec is a JoinedStr, not a Constant, so the
    # evaluator must recurse through it rather than dropping it silently.
    assert safe_eval("f'{n:.2f}'", scope={"n": 3.14159}) == "3.14"
    assert safe_eval("f'{n:05d}'", scope={"n": 42}) == "00042"
    # And the format spec itself can reference scope vars.
    assert safe_eval("f'{n:>{w}}'", scope={"n": 7, "w": 4}) == "   7"


def test_safe_eval_ternary_and_boolean_ops() -> None:
    assert safe_eval("'x' if cond else 'y'", scope={"cond": True}) == "x"
    assert safe_eval("a and b", scope={"a": 1, "b": 0}) == 0
    assert safe_eval("a or b", scope={"a": 0, "b": 5}) == 5


# -- safe_eval: rejects dangerous nodes --------------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os')",
        "().__class__.__base__",
        "lambda x: x",
        "[i for i in range(3)]",
        "{i for i in range(3)}",
        "{i: i for i in range(3)}",
        "(i for i in range(3))",
        "yield 1",
    ],
)
def test_safe_eval_rejects_dangerous_constructs(expression: str) -> None:
    with pytest.raises(ScriptError):
        safe_eval(expression, scope={})


def test_safe_eval_rejects_attribute_calls_to_force_helper_only_calls() -> None:
    # `obj.method(...)` would let us pivot off any value in scope - the helper
    # whitelist is the only intended call surface.
    with pytest.raises(ScriptError, match="named helper"):
        safe_eval("vars.upper()", scope={"vars": "hi"})


def test_safe_eval_rejects_dunder_attribute_access() -> None:
    with pytest.raises(ScriptError, match="forbidden attribute"):
        safe_eval("vars.__class__", scope={"vars": {}})


def test_safe_eval_rejects_unknown_helpers() -> None:
    with pytest.raises(ScriptError, match="unknown name"):
        safe_eval("eval('1+1')", scope={})


def test_safe_eval_rejects_star_unpacking() -> None:
    with pytest.raises(ScriptError):
        safe_eval("uuid_str(*[1])", scope={})


def test_safe_eval_rejects_dict_unpacking() -> None:
    # `{**other}` becomes ast.Dict with key=None.  Without explicit verify
    # rejection the entries were silently dropped, which violates the
    # "no unpacking" policy already enforced for function calls.
    with pytest.raises(ScriptError, match="dict literals"):
        safe_eval("{**other, 'a': 1}", scope={"other": {"b": 2}})


@pytest.mark.parametrize(
    "expression",
    [
        # Direct dunder traversal - the classic str.format escape pattern.
        "format_template('{x.__class__}', x=1)",
        # Nested dunder traversal would reach the int → object → subclasses
        # chain that hands out arbitrary classes from a plain str.format call.
        "format_template('{x.__class__.__bases__}', x=1)",
        # Item subscripts can also walk objects.
        "format_template('{x[0]}', x='abc')",
        # Underscore-prefixed names are reserved.
        "format_template('{_p}', _p=1)",
    ],
)
def test_format_template_blocks_attribute_walks(expression: str) -> None:
    with pytest.raises(ScriptError, match="format_template"):
        safe_eval(expression, scope={})


def test_format_template_still_works_for_plain_placeholders() -> None:
    assert (
        safe_eval("format_template('Hi {name}', name='alice')", scope={})
        == "Hi alice"
    )
    assert (
        safe_eval(
            "format_template('{greeting}, {name}!', greeting='hi', name='bob')",
            scope={},
        )
        == "hi, bob!"
    )


def test_safe_eval_wraps_native_runtime_errors_as_script_error() -> None:
    # ZeroDivisionError used to leak past safe_eval, breaking run_script_step
    # which only catches ScriptError.  All native runtime errors should be
    # surfaced as ScriptError so assertions degrade gracefully.
    with pytest.raises(ScriptError, match="evaluation error"):
        safe_eval("1 / 0", scope={})
    # `in` against a non-iterable raises TypeError - must also be wrapped.
    with pytest.raises(ScriptError, match="evaluation error"):
        safe_eval("'x' in 5", scope={})


# -- run_script_step ----------------------------------------------------------


def test_run_script_step_sets_variables_and_records_assertions() -> None:
    scope: dict = {}
    step = {
        "id": "compute",
        "set": {
            "token": "uuid_str()",
            "user_email": "f'user-{n}@test.example'",
        },
        "assert": ["len(token) > 0", "contains(user_email, '@test.example')"],
    }
    result = run_script_step(step, scope={**scope, "n": 42})
    assert result.error == ""
    assert isinstance(result.set_vars["token"], str)
    assert "user-42@test.example" == result.set_vars["user_email"]
    assert all(a["ok"] for a in result.assertions)


def test_run_script_step_records_failed_assertion_without_raising() -> None:
    result = run_script_step(
        {"set": {}, "assert": ["1 == 2", "len('x') > 5"]},
        scope={},
    )
    assert result.error == ""
    assert [a["ok"] for a in result.assertions] == [False, False]


def test_run_script_step_surfaces_set_expression_errors() -> None:
    result = run_script_step(
        {"set": {"x": "__import__('os')"}, "assert": []},
        scope={},
    )
    assert result.error.startswith("set x:")
    assert result.set_vars == {}


def test_run_script_step_rejects_invalid_variable_names() -> None:
    result = run_script_step(
        {"set": {"_secret": "1"}, "assert": []},
        scope={},
    )
    assert "invalid variable name" in result.error


def test_run_script_step_set_block_is_atomic_on_failure() -> None:
    """A failure mid-`set` must NOT leak earlier assignments into the run scope.

    Steps share the same scope dict across the run, so a partial commit
    here would mean a later step sees `vars.a` even though the script step
    that defined it returned an error.
    """
    scope: dict = {}
    result = run_script_step(
        {
            "set": {
                "a": "1",
                "b": "1 / 0",  # raises ScriptError, must abort the whole set
                "c": "3",
            },
            "assert": [],
        },
        scope=scope,
    )
    assert result.error.startswith("set b:")
    assert result.set_vars == {}
    # The shared scope must be untouched.  `vars` is allowed to exist
    # (setdefault creates it) but it must not contain `a`.
    assert scope.get("vars", {}) == {}


def test_run_script_step_records_runtime_error_as_failed_assertion() -> None:
    """A divide-by-zero in an assert must not abort the run."""
    result = run_script_step(
        {"set": {}, "assert": ["1 / 0 == 0"]},
        scope={},
    )
    assert result.error == ""
    assert result.assertions[0]["ok"] is False
    assert "evaluation error" in result.assertions[0]["message"]


def test_run_script_step_persists_vars_into_scope_for_subsequent_steps() -> None:
    scope: dict = {}
    run_script_step({"set": {"token": "'abc'"}, "assert": []}, scope=scope)
    assert scope["vars"]["token"] == "abc"
    # A second script step can read what the first one set.
    second = run_script_step(
        {"set": {}, "assert": ["vars.token == 'abc'"]},
        scope=scope,
    )
    assert second.assertions[0]["ok"] is True


# -- render_template ----------------------------------------------------------


def test_render_template_substitutes_top_level_and_dotted_paths() -> None:
    scope = {"vars": {"name": "alice", "id": 7}, "host": "example.com"}
    assert render_template("/users/{{ vars.id }}", scope) == "/users/7"
    assert render_template("//{{ host }}/x", scope) == "//example.com/x"


def test_render_template_returns_empty_for_missing_or_unsafe_keys() -> None:
    scope = {"vars": {"good": "ok"}}
    assert render_template("hi {{ vars.missing }}!", scope) == "hi !"
    # Dunders are blocked at render time too.
    assert render_template("{{ vars.__class__ }}", scope) == ""


def test_render_template_preserves_string_when_no_braces() -> None:
    assert render_template("plain", {}) == "plain"


# -- helper sanity checks ----------------------------------------------------


def test_default_helpers_are_safe_when_called_directly() -> None:
    # Smoke: every default helper is callable with documented args.
    assert isinstance(DEFAULT_SCRIPT_HELPERS["now_iso"](), str)
    assert isinstance(DEFAULT_SCRIPT_HELPERS["random_token"](), str)
    assert DEFAULT_SCRIPT_HELPERS["lower"]("ABC") == "abc"
    assert DEFAULT_SCRIPT_HELPERS["upper"]("abc") == "ABC"
