"""Scope / breadcrumb / context tests."""

from __future__ import annotations

import threading

from retrace_sdk.scope import Scope, merge_event_with_scope, push_scope


def test_breadcrumb_buffer_caps_at_configured_size():
    scope = Scope()
    scope.reconfigure(max_breadcrumbs=3)
    for i in range(10):
        scope.add_breadcrumb(category="c", message=f"m{i}")
    # FIFO truncation: oldest dropped.
    assert [b.message for b in scope.breadcrumbs] == ["m7", "m8", "m9"]


def test_set_user_filters_none_values():
    scope = Scope()
    scope.set_user({"id": "u1", "email": None, "name": "Ada"})
    assert scope.user == {"id": "u1", "name": "Ada"}


def test_set_user_none_clears():
    scope = Scope()
    scope.set_user({"id": "x"})
    scope.set_user(None)
    assert scope.user == {}


def test_set_tag_none_removes():
    scope = Scope()
    scope.set_tag("env", "prod")
    scope.set_tag("env", None)
    assert "env" not in scope.tags


def test_push_scope_isolates_changes():
    Scope.replace_current(Scope())
    outer = Scope.current()
    outer.set_tag("outer", "yes")
    with push_scope() as inner:
        assert inner is not outer
        inner.set_tag("inner", "yes")
        assert inner.tags == {"inner": "yes"}  # outer tag NOT inherited
    # outer restored, no leak of inner.
    assert Scope.current() is outer
    assert outer.tags == {"outer": "yes"}


def test_merge_event_with_scope_does_not_clobber_event_tags():
    scope = Scope()
    scope.set_tag("a", "from-scope")
    scope.set_tag("b", "from-scope")
    event = {"tags": {"b": "from-event"}}
    merged = merge_event_with_scope(event, scope)
    assert merged["tags"] == {"a": "from-scope", "b": "from-event"}


def test_merge_event_with_scope_combines_breadcrumbs():
    scope = Scope()
    scope.add_breadcrumb(category="c", message="from-scope")
    event = {"breadcrumbs": {"values": [{"message": "from-event"}]}}
    merged = merge_event_with_scope(event, scope)
    msgs = [b.get("message") for b in merged["breadcrumbs"]["values"]]
    assert msgs == ["from-scope", "from-event"]


def test_scope_isolation_across_threads():
    """Each thread sees its own scope; we use contextvars semantics."""
    Scope.replace_current(Scope())
    main_scope = Scope.current()
    main_scope.set_tag("thread", "main")

    other_thread_tags: list = []

    def _worker():
        # New thread → new scope (default factory).
        Scope.replace_current(Scope())
        s = Scope.current()
        s.set_tag("thread", "worker")
        other_thread_tags.append(dict(s.tags))

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    assert other_thread_tags == [{"thread": "worker"}]
    assert main_scope.tags == {"thread": "main"}
