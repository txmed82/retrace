"""OpenAPI contract diff (P1.4 — scope-trimmed).

`retrace tester api-diff --new openapi.yaml --old openapi.prev.yaml`
emits a list of `BreakingChange`s and (optionally) files one
`qa_incident` per change so the same `qa list` / `qa auto` rails
that already handle replay / UI / API-test failures also catch
contract regressions.

What we classify as breaking (the OpenAPI / oas-tools community
consensus):

  * **operation_removed** — an `{method, path}` pair the old spec
    defined and the new one doesn't.
  * **required_request_field_added** — a request property that used
    to be optional (or absent) is now required.
  * **response_schema_field_removed** — a field clients could rely
    on in 2xx responses is gone from the new spec.
  * **success_status_removed** — a 2xx response status the old spec
    declared has no replacement.
  * **enum_value_removed** — an enum member the old spec listed is
    no longer in the new spec; clients that branch on it break.

What we classify as **safe** (and surface separately so reviewers see
the surface area without filing incidents):

  * `operation_added`, `optional_field_added`, `enum_value_added`.

The diff is **structural only** — we don't follow `$ref` cross-spec.
Schemas embedded under `components.schemas` ARE followed within a
single document (so a request that references `#/components/schemas/User`
expands), but only one level of indirection. That covers the
overwhelming majority of real-world specs without bringing in a full
JSON Schema resolver.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


# Order matters — we surface "operation removed" first so the
# `--limit` truncation keeps the most disruptive change visible.
BREAKING_KINDS = (
    "operation_removed",
    "required_request_field_added",
    "response_schema_field_removed",
    "success_status_removed",
    "enum_value_removed",
)

SAFE_KINDS = (
    "operation_added",
    "optional_request_field_added",
    "response_schema_field_added",
    "enum_value_added",
)


@dataclass(frozen=True)
class ContractChange:
    """One per-operation contract change.

    `kind` is one of the constants above. `severity` is `"breaking"`
    for `BREAKING_KINDS` and `"safe"` otherwise.
    """

    kind: str
    severity: str
    path: str
    method: str
    field_path: str = ""
    detail: str = ""
    old_value: Any = None
    new_value: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def title(self) -> str:
        if self.kind == "operation_removed":
            return f"OpenAPI: {self.method} {self.path} removed"
        if self.kind == "operation_added":
            return f"OpenAPI: {self.method} {self.path} added"
        if self.field_path:
            return (
                f"OpenAPI {self.method} {self.path}: "
                f"{self.kind.replace('_', ' ')} ({self.field_path})"
            )
        return f"OpenAPI {self.method} {self.path}: {self.kind.replace('_', ' ')}"


@dataclass(frozen=True)
class ContractDiffResult:
    breaking: list[ContractChange] = field(default_factory=list)
    safe: list[ContractChange] = field(default_factory=list)

    @property
    def has_breaking(self) -> bool:
        return bool(self.breaking)

    def to_dict(self) -> dict[str, Any]:
        return {
            "breaking": [c.to_dict() for c in self.breaking],
            "safe": [c.to_dict() for c in self.safe],
        }


# ---------------------------------------------------------------------------
# Top-level diff
# ---------------------------------------------------------------------------


def diff_openapi_documents(
    *,
    old: dict[str, Any],
    new: dict[str, Any],
) -> ContractDiffResult:
    """Compute the set of contract changes between two parsed OpenAPI
    documents.

    `old` and `new` are the dicts returned by `load_openapi_document`
    (or any function that returns a parsed OpenAPI 3.x / Swagger 2.x
    document).
    """
    old_paths = old.get("paths") if isinstance(old.get("paths"), dict) else {}
    new_paths = new.get("paths") if isinstance(new.get("paths"), dict) else {}

    breaking: list[ContractChange] = []
    safe: list[ContractChange] = []

    old_ops = _collect_operations(old_paths)
    new_ops = _collect_operations(new_paths)
    old_keys = set(old_ops)
    new_keys = set(new_ops)

    # Operation-level: removed / added.
    for key in sorted(old_keys - new_keys):
        path, method = key
        breaking.append(
            ContractChange(
                kind="operation_removed",
                severity="breaking",
                path=path,
                method=method,
                detail="Operation removed from new spec.",
            )
        )
    for key in sorted(new_keys - old_keys):
        path, method = key
        safe.append(
            ContractChange(
                kind="operation_added",
                severity="safe",
                path=path,
                method=method,
                detail="New operation in new spec.",
            )
        )

    # Field-level diffs on the intersection.
    for key in sorted(old_keys & new_keys):
        path, method = key
        _diff_operation(
            old_doc=old,
            new_doc=new,
            old_op=old_ops[key],
            new_op=new_ops[key],
            path=path,
            method=method,
            breaking=breaking,
            safe=safe,
        )

    return ContractDiffResult(breaking=breaking, safe=safe)


def _collect_operations(
    paths: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Return `{(path, METHOD): operation_dict}` for every method on
    every path."""
    out: dict[tuple[str, str], dict[str, Any]] = {}
    methods = {"get", "post", "put", "patch", "delete", "options", "head"}
    if not isinstance(paths, dict):
        return out
    for raw_path, operations in paths.items():
        if not isinstance(operations, dict):
            continue
        for method, op in operations.items():
            if not isinstance(op, dict):
                continue
            if method.lower() not in methods:
                continue
            out[(str(raw_path), method.upper())] = op
    return out


# ---------------------------------------------------------------------------
# Operation diff
# ---------------------------------------------------------------------------


def _diff_operation(
    *,
    old_doc: dict[str, Any],
    new_doc: dict[str, Any],
    old_op: dict[str, Any],
    new_op: dict[str, Any],
    path: str,
    method: str,
    breaking: list[ContractChange],
    safe: list[ContractChange],
) -> None:
    _diff_request_body(
        old_doc=old_doc, new_doc=new_doc,
        old_op=old_op, new_op=new_op,
        path=path, method=method,
        breaking=breaking, safe=safe,
    )
    _diff_responses(
        old_doc=old_doc, new_doc=new_doc,
        old_op=old_op, new_op=new_op,
        path=path, method=method,
        breaking=breaking, safe=safe,
    )


def _diff_request_body(
    *,
    old_doc, new_doc,
    old_op, new_op,
    path, method,
    breaking, safe,
) -> None:
    old_schema = _request_schema(old_doc, old_op)
    new_schema = _request_schema(new_doc, new_op)
    if old_schema is None and new_schema is None:
        return
    old_required = set(_required_fields(old_schema or {}))
    new_required = set(_required_fields(new_schema or {}))
    old_props = _properties(old_schema or {})
    new_props = _properties(new_schema or {})

    # New required field that wasn't required before — breaks clients.
    for field_name in sorted(new_required - old_required):
        breaking.append(
            ContractChange(
                kind="required_request_field_added",
                severity="breaking",
                path=path,
                method=method,
                field_path=f"request.body.{field_name}",
                detail=(
                    "Field is required in new spec; clients omitting "
                    "it will get a 4xx."
                ),
            )
        )
    # New optional field — safe addition (still surface as a change).
    for field_name in sorted(set(new_props) - set(old_props) - new_required):
        safe.append(
            ContractChange(
                kind="optional_request_field_added",
                severity="safe",
                path=path,
                method=method,
                field_path=f"request.body.{field_name}",
                detail="Optional field added.",
            )
        )

    # Enum diffs on shared properties (request body).
    for field_name in sorted(set(old_props) & set(new_props)):
        _diff_enum(
            old=old_props[field_name],
            new=new_props[field_name],
            path=path, method=method,
            field_path=f"request.body.{field_name}",
            breaking=breaking, safe=safe,
        )


def _diff_responses(
    *,
    old_doc, new_doc,
    old_op, new_op,
    path, method,
    breaking, safe,
) -> None:
    old_responses = _dict_or_empty(old_op.get("responses"))
    new_responses = _dict_or_empty(new_op.get("responses"))

    # Removed 2xx status codes (breaking — a client may key on `data.id`
    # that lived under a 201 that's now gone).
    success = lambda s: str(s).startswith("2") or str(s).lower() == "default"  # noqa: E731
    old_success = {k for k in old_responses if success(k)}
    new_success = {k for k in new_responses if success(k)}
    for code in sorted(old_success - new_success):
        breaking.append(
            ContractChange(
                kind="success_status_removed",
                severity="breaking",
                path=path,
                method=method,
                field_path=f"responses.{code}",
                old_value=code,
                detail="Success status removed; clients keying on its body break.",
            )
        )

    # Per-status response-schema field diff (on shared 2xx codes).
    for code in sorted(old_success & new_success):
        old_schema = _response_schema(old_doc, old_responses[code])
        new_schema = _response_schema(new_doc, new_responses[code])
        if old_schema is None and new_schema is None:
            continue
        old_props = _properties(old_schema or {})
        new_props = _properties(new_schema or {})
        for field_name in sorted(set(old_props) - set(new_props)):
            breaking.append(
                ContractChange(
                    kind="response_schema_field_removed",
                    severity="breaking",
                    path=path,
                    method=method,
                    field_path=f"responses.{code}.{field_name}",
                    detail=(
                        "Response field removed; clients reading it will "
                        "see undefined."
                    ),
                )
            )
        for field_name in sorted(set(new_props) - set(old_props)):
            safe.append(
                ContractChange(
                    kind="response_schema_field_added",
                    severity="safe",
                    path=path,
                    method=method,
                    field_path=f"responses.{code}.{field_name}",
                    detail="Response field added (safe — additive).",
                )
            )
        for field_name in sorted(set(old_props) & set(new_props)):
            _diff_enum(
                old=old_props[field_name],
                new=new_props[field_name],
                path=path, method=method,
                field_path=f"responses.{code}.{field_name}",
                breaking=breaking, safe=safe,
            )


def _diff_enum(
    *,
    old: dict[str, Any],
    new: dict[str, Any],
    path: str,
    method: str,
    field_path: str,
    breaking: list[ContractChange],
    safe: list[ContractChange],
) -> None:
    old_enum = old.get("enum") if isinstance(old, dict) else None
    new_enum = new.get("enum") if isinstance(new, dict) else None
    if not isinstance(old_enum, list) or not isinstance(new_enum, list):
        return
    old_set = set(old_enum)
    new_set = set(new_enum)
    for v in sorted(old_set - new_set, key=lambda x: str(x)):
        breaking.append(
            ContractChange(
                kind="enum_value_removed",
                severity="breaking",
                path=path,
                method=method,
                field_path=f"{field_path}.enum",
                old_value=v,
                detail=f"Enum value {v!r} removed; clients branching on it break.",
            )
        )
    for v in sorted(new_set - old_set, key=lambda x: str(x)):
        safe.append(
            ContractChange(
                kind="enum_value_added",
                severity="safe",
                path=path,
                method=method,
                field_path=f"{field_path}.enum",
                new_value=v,
                detail=f"Enum value {v!r} added.",
            )
        )


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------


def _request_schema(doc: dict[str, Any], op: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Best-effort request-body schema lookup.

    OpenAPI 3: `requestBody.content[<media>].schema`. Prefers
    `application/json`; falls back to the first content type.

    Swagger 2: body parameters live in `parameters: [{in: "body",
    schema: {...}}]`. Falls back to that when `requestBody` is absent.
    (CodeRabbit Major catch on PR #134.)
    """
    rb = op.get("requestBody")
    if isinstance(rb, dict):
        content = rb.get("content") if isinstance(rb.get("content"), dict) else {}
        if "application/json" in content:
            return _resolve_schema(doc, content["application/json"].get("schema"))
        for media in content.values():
            if isinstance(media, dict) and "schema" in media:
                return _resolve_schema(doc, media["schema"])
        return None
    # Swagger 2 fallback.
    params = op.get("parameters")
    if isinstance(params, list):
        for p in params:
            if isinstance(p, dict) and str(p.get("in")) == "body" and "schema" in p:
                return _resolve_schema(doc, p.get("schema"))
    return None


def _response_schema(doc: dict[str, Any], response: Any) -> Optional[dict[str, Any]]:
    if not isinstance(response, dict):
        return None
    content = response.get("content") if isinstance(response.get("content"), dict) else {}
    if "application/json" in content:
        return _resolve_schema(doc, content["application/json"].get("schema"))
    # Swagger 2.x put the schema at `response.schema`.
    if "schema" in response:
        return _resolve_schema(doc, response.get("schema"))
    for media in content.values():
        if isinstance(media, dict) and "schema" in media:
            return _resolve_schema(doc, media["schema"])
    return None


def _resolve_schema(
    doc: dict[str, Any],
    schema: Any,
    *,
    depth: int = 0,
) -> Optional[dict[str, Any]]:
    """One-level `$ref` resolution. Deeper indirection is left as a
    raw `$ref` dict — diff is structural-only."""
    if not isinstance(schema, dict):
        return None
    if depth > 1:
        return schema
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        target = _walk_ref(doc, ref[2:].split("/"))
        if isinstance(target, dict):
            return _resolve_schema(doc, target, depth=depth + 1)
    return schema


def _walk_ref(root: dict[str, Any], parts: Iterable[str]) -> Any:
    node: Any = root
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _properties(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties")
    if not isinstance(props, dict):
        return {}
    return {str(k): v if isinstance(v, dict) else {} for k, v in props.items()}


def _required_fields(schema: dict[str, Any]) -> list[str]:
    if not isinstance(schema, dict):
        return []
    required = schema.get("required")
    if not isinstance(required, list):
        return []
    return [str(item) for item in required if isinstance(item, str)]


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# Doc loading (mirrors `openapi_import.load_openapi_document` but
# without the strict `paths must exist` requirement — a brand-new
# spec might add paths to a previously-empty document).
# ---------------------------------------------------------------------------


def load_openapi(path: Path) -> dict[str, Any]:
    """Load an OpenAPI / Swagger document from JSON or YAML.

    Parse errors (malformed JSON or YAML) are normalized to `ValueError`
    so the CLI wrapper can surface a clean `ClickException` instead of
    a stack trace. (CodeRabbit Major catch on PR #134.)
    """
    import yaml

    raw = Path(path).read_text(encoding="utf-8")
    try:
        if Path(path).suffix.lower() == ".json":
            data = json.loads(raw)
        else:
            data = yaml.safe_load(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse OpenAPI document at {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Failed to parse OpenAPI document at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"OpenAPI document at {path} must be a YAML/JSON object")
    if "openapi" not in data and "swagger" not in data:
        raise ValueError(
            f"OpenAPI document at {path} must include `openapi` or `swagger`"
        )
    return data


__all__ = [
    "BREAKING_KINDS",
    "SAFE_KINDS",
    "ContractChange",
    "ContractDiffResult",
    "diff_openapi_documents",
    "load_openapi",
]
