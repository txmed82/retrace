"""OpenAPI contract-diff tests (P1.4).

Pins the breaking-vs-safe classification + the matchers we rely on
in `retrace tester api-diff`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from retrace.openapi_diff import (
    BREAKING_KINDS,
    SAFE_KINDS,
    ContractChange,
    diff_openapi_documents,
    load_openapi,
)


# ---------------------------------------------------------------------------
# Document builders — small enough to inline so tests read top-to-bottom.
# ---------------------------------------------------------------------------


def _doc(*operations: tuple[str, str, dict]) -> dict:
    """Build a minimal OpenAPI 3 doc from `(path, method, operation)` tuples."""
    paths: dict[str, dict] = {}
    for path, method, op in operations:
        paths.setdefault(path, {})[method.lower()] = op
    return {"openapi": "3.0.0", "info": {"title": "x", "version": "1"}, "paths": paths}


def _req_body_schema(props: dict, required: list[str] = None) -> dict:
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return {
        "requestBody": {
            "content": {"application/json": {"schema": schema}},
        }
    }


def _response_schema(status: str, props: dict) -> dict:
    return {
        "responses": {
            status: {
                "content": {
                    "application/json": {
                        "schema": {"type": "object", "properties": props}
                    }
                }
            }
        }
    }


# ---------------------------------------------------------------------------
# Operation-level diff
# ---------------------------------------------------------------------------


def test_operation_removed_is_breaking():
    old = _doc(("/users", "GET", {}))
    new = _doc()
    diff = diff_openapi_documents(old=old, new=new)
    assert [c.kind for c in diff.breaking] == ["operation_removed"]
    assert diff.breaking[0].method == "GET"
    assert diff.breaking[0].path == "/users"
    assert diff.safe == []


def test_operation_added_is_safe():
    old = _doc()
    new = _doc(("/users", "POST", {}))
    diff = diff_openapi_documents(old=old, new=new)
    assert diff.breaking == []
    assert [c.kind for c in diff.safe] == ["operation_added"]
    assert diff.safe[0].method == "POST"


# ---------------------------------------------------------------------------
# Request body diffs
# ---------------------------------------------------------------------------


def test_required_request_field_added_is_breaking():
    """A field that was previously optional becoming required is the
    classic backwards-incompatible change."""
    old = _doc(("/u", "POST", _req_body_schema({"name": {"type": "string"}})))
    new = _doc(
        ("/u", "POST", _req_body_schema({"name": {"type": "string"}}, required=["name"]))
    )
    diff = diff_openapi_documents(old=old, new=new)
    assert [c.kind for c in diff.breaking] == ["required_request_field_added"]
    assert diff.breaking[0].field_path == "request.body.name"


def test_optional_request_field_added_is_safe():
    old = _doc(("/u", "POST", _req_body_schema({"a": {"type": "string"}})))
    new = _doc(
        ("/u", "POST", _req_body_schema({
            "a": {"type": "string"},
            "b": {"type": "string"},
        }))
    )
    diff = diff_openapi_documents(old=old, new=new)
    assert diff.breaking == []
    assert [c.kind for c in diff.safe] == ["optional_request_field_added"]
    assert diff.safe[0].field_path == "request.body.b"


def test_new_required_field_alongside_existing_optional_only_flags_the_new_one():
    """The first existing field doesn't become required — only the new one."""
    old = _doc(("/u", "POST", _req_body_schema({"a": {"type": "string"}})))
    new = _doc(
        ("/u", "POST", _req_body_schema(
            {"a": {"type": "string"}, "b": {"type": "string"}},
            required=["b"],
        ))
    )
    diff = diff_openapi_documents(old=old, new=new)
    assert [c.field_path for c in diff.breaking] == ["request.body.b"]


# ---------------------------------------------------------------------------
# Response diffs
# ---------------------------------------------------------------------------


def test_response_schema_field_removed_is_breaking():
    old_op = _response_schema("200", {"id": {"type": "string"}, "name": {"type": "string"}})
    new_op = _response_schema("200", {"id": {"type": "string"}})
    diff = diff_openapi_documents(old=_doc(("/u", "GET", old_op)), new=_doc(("/u", "GET", new_op)))
    assert [c.kind for c in diff.breaking] == ["response_schema_field_removed"]
    assert diff.breaking[0].field_path == "responses.200.name"


def test_response_schema_field_added_is_safe():
    old_op = _response_schema("200", {"id": {"type": "string"}})
    new_op = _response_schema("200", {"id": {"type": "string"}, "name": {"type": "string"}})
    diff = diff_openapi_documents(old=_doc(("/u", "GET", old_op)), new=_doc(("/u", "GET", new_op)))
    assert diff.breaking == []
    assert [c.kind for c in diff.safe] == ["response_schema_field_added"]


def test_success_status_removed_is_breaking():
    old_op = {"responses": {"201": {"description": "created"}}}
    new_op = {"responses": {"200": {"description": "ok"}}}
    diff = diff_openapi_documents(old=_doc(("/u", "POST", old_op)), new=_doc(("/u", "POST", new_op)))
    kinds = [c.kind for c in diff.breaking]
    assert "success_status_removed" in kinds


# ---------------------------------------------------------------------------
# Enum diffs
# ---------------------------------------------------------------------------


def test_enum_value_removed_in_request_body_is_breaking():
    old_op = _req_body_schema(
        {"status": {"type": "string", "enum": ["a", "b", "c"]}},
    )
    new_op = _req_body_schema(
        {"status": {"type": "string", "enum": ["a", "b"]}},
    )
    diff = diff_openapi_documents(old=_doc(("/u", "POST", old_op)), new=_doc(("/u", "POST", new_op)))
    assert [c.kind for c in diff.breaking] == ["enum_value_removed"]
    assert diff.breaking[0].old_value == "c"


def test_enum_value_added_is_safe():
    old_op = _req_body_schema({"status": {"type": "string", "enum": ["a"]}})
    new_op = _req_body_schema({"status": {"type": "string", "enum": ["a", "b"]}})
    diff = diff_openapi_documents(old=_doc(("/u", "POST", old_op)), new=_doc(("/u", "POST", new_op)))
    assert diff.breaking == []
    assert [c.kind for c in diff.safe] == ["enum_value_added"]


def test_enum_value_removed_in_response_schema_is_breaking():
    old_op = _response_schema("200", {"role": {"type": "string", "enum": ["admin", "user", "guest"]}})
    new_op = _response_schema("200", {"role": {"type": "string", "enum": ["admin", "user"]}})
    diff = diff_openapi_documents(old=_doc(("/u", "GET", old_op)), new=_doc(("/u", "GET", new_op)))
    assert [c.kind for c in diff.breaking] == ["enum_value_removed"]
    assert diff.breaking[0].field_path == "responses.200.role.enum"


# ---------------------------------------------------------------------------
# $ref resolution
# ---------------------------------------------------------------------------


def test_ref_one_level_resolves_for_request_body():
    """A request body that `$ref`s a component schema should still
    diff against a different shape."""
    old = {
        "openapi": "3.0.0",
        "info": {"title": "x", "version": "1"},
        "components": {
            "schemas": {
                "Login": {"type": "object", "properties": {"email": {"type": "string"}}}
            }
        },
        "paths": {
            "/login": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Login"}}
                        }
                    }
                }
            }
        },
    }
    new = {
        "openapi": "3.0.0",
        "info": {"title": "x", "version": "1"},
        "components": {
            "schemas": {
                "Login": {
                    "type": "object",
                    "properties": {"email": {"type": "string"}, "password": {"type": "string"}},
                    "required": ["password"],
                }
            }
        },
        "paths": {
            "/login": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Login"}}
                        }
                    }
                }
            }
        },
    }
    diff = diff_openapi_documents(old=old, new=new)
    assert [c.kind for c in diff.breaking] == ["required_request_field_added"]
    assert diff.breaking[0].field_path == "request.body.password"


# ---------------------------------------------------------------------------
# Determinism + shape
# ---------------------------------------------------------------------------


def test_results_are_deterministic_across_runs():
    """Identical inputs produce identical outputs in identical order."""
    old = _doc(
        ("/a", "GET", _response_schema("200", {"x": {"type": "string"}, "y": {"type": "string"}})),
        ("/b", "POST", _req_body_schema({"z": {"type": "string"}})),
    )
    new = _doc(
        ("/a", "GET", _response_schema("200", {})),
        ("/b", "POST", _req_body_schema({}, required=[])),  # noop
    )
    a = diff_openapi_documents(old=old, new=new)
    b = diff_openapi_documents(old=old, new=new)
    assert [c.to_dict() for c in a.breaking] == [c.to_dict() for c in b.breaking]


def test_change_has_severity_per_kind():
    """Every kind in BREAKING_KINDS maps to severity='breaking';
    SAFE_KINDS maps to severity='safe'. This catches accidental
    severity-typo drift."""
    old = _doc(
        ("/a", "GET", _response_schema("200", {"x": {"type": "string"}})),
        ("/dead", "POST", {}),
    )
    new = _doc(
        ("/a", "GET", _response_schema("200", {})),
        ("/b", "POST", _req_body_schema({"opt": {"type": "string"}})),
    )
    diff = diff_openapi_documents(old=old, new=new)
    for c in diff.breaking:
        assert c.kind in BREAKING_KINDS
        assert c.severity == "breaking"
    for c in diff.safe:
        assert c.kind in SAFE_KINDS
        assert c.severity == "safe"


def test_to_dict_is_json_serializable():
    """The full diff result has to survive JSON serialization for the
    CLI's `--json` flag."""
    old = _doc(("/a", "GET", {}))
    new = _doc()
    diff = diff_openapi_documents(old=old, new=new)
    payload = diff.to_dict()
    assert json.dumps(payload)  # raises if non-serializable


def test_empty_paths_does_not_crash():
    diff = diff_openapi_documents(old={"openapi": "3.0", "paths": {}}, new={"openapi": "3.0"})
    assert diff.breaking == []
    assert diff.safe == []


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def test_load_openapi_accepts_json_and_yaml(tmp_path: Path):
    spec_json = tmp_path / "x.json"
    spec_json.write_text(json.dumps({"openapi": "3.0", "paths": {}}))
    assert load_openapi(spec_json)["openapi"] == "3.0"
    spec_yaml = tmp_path / "x.yaml"
    spec_yaml.write_text("openapi: '3.0'\npaths: {}\n")
    assert load_openapi(spec_yaml)["openapi"] == "3.0"


def test_load_openapi_rejects_non_openapi_documents(tmp_path: Path):
    spec = tmp_path / "x.json"
    spec.write_text(json.dumps({"hello": "world"}))
    with pytest.raises(ValueError, match="openapi"):
        load_openapi(spec)


def test_load_openapi_rejects_non_object_root(tmp_path: Path):
    spec = tmp_path / "x.yaml"
    spec.write_text("- not\n- an\n- object\n")
    with pytest.raises(ValueError, match="object"):
        load_openapi(spec)


# ---------------------------------------------------------------------------
# ContractChange title surface
# ---------------------------------------------------------------------------


def test_title_for_operation_removed():
    c = ContractChange(
        kind="operation_removed",
        severity="breaking",
        path="/users/{id}",
        method="DELETE",
    )
    assert c.title == "OpenAPI: DELETE /users/{id} removed"


def test_title_for_field_change():
    c = ContractChange(
        kind="required_request_field_added",
        severity="breaking",
        path="/u",
        method="POST",
        field_path="request.body.email",
    )
    assert "required request field added" in c.title
    assert "request.body.email" in c.title
