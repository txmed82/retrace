from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import yaml

from retrace.api_suites import APITestSuite, create_api_suite
from retrace.api_testing import APITestSpec, create_api_spec


OPENAPI_IMPORT_VERSION = 1
SUPPORTED_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


@dataclass
class OpenAPIImportResult:
    specs: list[APITestSpec]
    skipped: list[str]
    suite: APITestSuite | None = None
    quality_report: dict[str, Any] | None = None


def import_openapi_specs(
    *,
    openapi_path: Path,
    specs_dir: Path,
    suites_dir: Path | None = None,
    base_url: str,
    path_filter: str = "",
    method_filter: str = "",
    auth_profile: str = "",
    env_profile: str = "",
    env_overrides: dict[str, str] | None = None,
) -> OpenAPIImportResult:
    document = load_openapi_document(openapi_path)
    base = _normalize_base_url(base_url or _server_url(document))
    if not base:
        raise ValueError("base_url is required when the OpenAPI document has no server URL")

    path_re = re.compile(path_filter) if path_filter.strip() else None
    wanted_method = method_filter.strip().lower()
    if wanted_method and wanted_method not in SUPPORTED_METHODS:
        raise ValueError(f"unsupported method filter: {method_filter}")

    specs: list[APITestSpec] = []
    skipped: list[str] = []
    operations: list[dict[str, Any]] = []
    total_operations = 0
    for raw_path, path_item in sorted((document.get("paths") or {}).items()):
        if not isinstance(path_item, dict):
            skipped.append(f"{raw_path}: path item is not an object")
            continue
        if path_re and not path_re.search(str(raw_path)):
            continue
        inherited_parameters = _parameters(document, path_item.get("parameters"))
        for method, operation in sorted(path_item.items()):
            method_l = str(method).lower()
            if method_l not in SUPPORTED_METHODS:
                continue
            total_operations += 1
            if wanted_method and method_l != wanted_method:
                continue
            if not isinstance(operation, dict):
                skipped.append(f"{method_l.upper()} {raw_path}: operation is not an object")
                continue
            try:
                spec = _operation_to_spec(
                    document=document,
                    specs_dir=specs_dir,
                    base_url=base,
                    raw_path=str(raw_path),
                    method=method_l.upper(),
                    path_parameters=inherited_parameters,
                    operation=operation,
                    auth_profile=auth_profile,
                    env_profile=env_profile,
                    env_overrides=dict(env_overrides or {}),
                )
                specs.append(spec)
                operations.append(_operation_manifest(spec=spec, operation=operation))
            except Exception as exc:
                skipped.append(f"{method_l.upper()} {raw_path}: {exc}")
    quality_report = _quality_report(
        document=document,
        imported_count=len(specs),
        skipped=skipped,
        total_operations=total_operations,
        operations=operations,
        path_filter=path_filter,
        method_filter=method_filter,
        auth_profile=auth_profile,
        env_profile=env_profile,
    )
    suite = create_api_suite(
        suites_dir=suites_dir or specs_dir.parent / "suites",
        name=f"OpenAPI {str((document.get('info') or {}).get('title') or openapi_path.stem)}",
        source="openapi_import",
        spec_ids=[spec.spec_id for spec in specs],
        filters={"path_filter": path_filter, "method": method_filter},
        auth_profile=auth_profile,
        env_profile=env_profile,
        import_summary=quality_report,
        operations=operations,
        skipped=skipped,
        metadata={
            "openapi_path": str(openapi_path),
            "base_url": base,
            "document_title": str((document.get("info") or {}).get("title") or ""),
            "document_version": str((document.get("info") or {}).get("version") or ""),
            "openapi_version": str(document.get("openapi") or document.get("swagger") or ""),
        },
    )
    return OpenAPIImportResult(
        specs=specs,
        skipped=skipped,
        suite=suite,
        quality_report=quality_report,
    )


def load_openapi_document(path: Path) -> dict[str, Any]:
    raw = path.read_text()
    data = json.loads(raw) if path.suffix.lower() == ".json" else yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError("OpenAPI document must be an object")
    if "openapi" not in data and "swagger" not in data:
        raise ValueError("OpenAPI document must include openapi or swagger")
    if not isinstance(data.get("paths"), dict):
        raise ValueError("OpenAPI document must include paths")
    return data


def _operation_to_spec(
    *,
    document: dict[str, Any],
    specs_dir: Path,
    base_url: str,
    raw_path: str,
    method: str,
    path_parameters: list[dict[str, Any]],
    operation: dict[str, Any],
    auth_profile: str = "",
    env_profile: str = "",
    env_overrides: dict[str, str] | None = None,
) -> APITestSpec:
    operation_parameters = path_parameters + _parameters(document, operation.get("parameters"))
    request_path = _render_path(raw_path, operation_parameters)
    query = _query_defaults(operation_parameters)
    status, response_schema = _response_contract(document, operation)
    body, content_type = _request_body_example(document, operation)
    operation_id = str(operation.get("operationId") or "").strip()
    summary = str(operation.get("summary") or "").strip()
    tags = [str(item) for item in list(operation.get("tags") or []) if str(item).strip()]
    name = summary or operation_id or f"{method} {raw_path}"
    return create_api_spec(
        specs_dir=specs_dir,
        name=f"OpenAPI {name}",
        method=method,
        url=_join_url(base_url, request_path),
        query=query,
        headers={"Content-Type": content_type} if body is not None and content_type else {},
        body=body,
        auth_profile=auth_profile,
        env_profile=env_profile,
        env_overrides=env_overrides or {},
        expected_status=status,
        schema_assertions=[{"schema": response_schema}] if response_schema else [],
        fixtures={
            "source": "openapi_import",
            "contract_derived": True,
            "openapi_import_version": OPENAPI_IMPORT_VERSION,
            "operation_id": operation_id,
            "summary": summary,
            "tags": tags,
            "deprecated": bool(operation.get("deprecated")),
            "security": operation.get("security", []),
            "openapi_path": raw_path,
            "openapi_method": method,
            "selected_response_status": str(status),
            "request_body_content_type": content_type,
        },
    )


def _operation_manifest(
    *,
    spec: APITestSpec,
    operation: dict[str, Any],
) -> dict[str, Any]:
    fixtures = dict(spec.fixtures or {})
    return {
        "spec_id": spec.spec_id,
        "method": spec.method,
        "path": fixtures.get("openapi_path", ""),
        "url": spec.url,
        "operation_id": fixtures.get("operation_id", ""),
        "summary": fixtures.get("summary", ""),
        "tags": fixtures.get("tags", []),
        "deprecated": bool(fixtures.get("deprecated")),
        "security": fixtures.get("security", []),
        "expected_status": spec.expected_status,
        "request_body": spec.body is not None,
        "request_body_content_type": fixtures.get("request_body_content_type", ""),
        "schema_assertion_count": len(spec.schema_assertions),
        "query_defaults": sorted(spec.query.keys()),
        "quality": {
            "has_operation_id": bool(fixtures.get("operation_id")),
            "has_response_schema": bool(spec.schema_assertions),
            "has_request_example": spec.body is not None,
            "has_summary": bool(fixtures.get("summary")),
            "has_security": bool(operation.get("security")),
        },
    }


def _quality_report(
    *,
    document: dict[str, Any],
    imported_count: int,
    skipped: list[str],
    total_operations: int,
    operations: list[dict[str, Any]],
    path_filter: str,
    method_filter: str,
    auth_profile: str,
    env_profile: str,
) -> dict[str, Any]:
    missing_operation_ids = [
        item["spec_id"] for item in operations if not item["quality"]["has_operation_id"]
    ]
    missing_response_schemas = [
        item["spec_id"] for item in operations if not item["quality"]["has_response_schema"]
    ]
    request_bodies_without_examples = [
        item["spec_id"]
        for item in operations
        if item["request_body"] and not item["quality"]["has_request_example"]
    ]
    selected_operations = imported_count + len(skipped)
    return {
        "openapi_import_version": OPENAPI_IMPORT_VERSION,
        "document_title": str((document.get("info") or {}).get("title") or ""),
        "document_version": str((document.get("info") or {}).get("version") or ""),
        "total_operations": total_operations,
        "selected_operations": selected_operations,
        "imported_count": imported_count,
        "skipped_count": len(skipped),
        "coverage_percent": round((imported_count / selected_operations) * 100, 2)
        if selected_operations
        else 0.0,
        "filters": {
            "path_filter": path_filter,
            "method": method_filter,
        },
        "profiles": {
            "auth_profile": auth_profile,
            "env_profile": env_profile,
        },
        "quality_warnings": {
            "missing_operation_ids": missing_operation_ids,
            "missing_response_schemas": missing_response_schemas,
            "request_bodies_without_examples": request_bodies_without_examples,
        },
    }


def _request_body_example(
    document: dict[str, Any],
    operation: dict[str, Any],
) -> tuple[Any, str]:
    request_body = _resolve_ref(document, operation.get("requestBody"))
    if not isinstance(request_body, dict):
        return None, ""
    content = request_body.get("content") or {}
    if not isinstance(content, dict) or not content:
        return None, ""
    content_type = (
        "application/json"
        if "application/json" in content
        else str(next(iter(content.keys())))
    )
    media = content.get(content_type)
    if not isinstance(media, dict):
        return None, content_type
    if "example" in media:
        return media["example"], content_type
    examples = media.get("examples")
    if isinstance(examples, dict) and examples:
        first = next(iter(examples.values()))
        if isinstance(first, dict) and "value" in first:
            return first["value"], content_type
    schema = _resolve_schema_refs(document, media.get("schema"), keep_examples=True)
    if isinstance(schema, dict):
        return _schema_example(schema), content_type
    return None, content_type


def _parameters(document: dict[str, Any], value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        resolved = _resolve_ref(document, item)
        if isinstance(resolved, dict):
            out.append(resolved)
    return out


def _render_path(raw_path: str, parameters: list[dict[str, Any]]) -> str:
    values = {
        str(param.get("name")): _parameter_example(param)
        for param in parameters
        if str(param.get("in") or "").lower() == "path"
    }

    def repl(match: re.Match[str]) -> str:
        value = values.get(match.group(1))
        return "1" if value is None else str(value)

    return re.sub(r"\{([^}]+)\}", repl, raw_path)


def _query_defaults(parameters: list[dict[str, Any]]) -> dict[str, Any]:
    query: dict[str, Any] = {}
    for param in parameters:
        if str(param.get("in") or "").lower() != "query":
            continue
        name = str(param.get("name") or "").strip()
        if not name or not bool(param.get("required")):
            continue
        query[name] = _parameter_example(param)
    return query


def _parameter_example(param: dict[str, Any]) -> Any:
    if "example" in param:
        return param["example"]
    examples = param.get("examples")
    if isinstance(examples, dict) and examples:
        first = next(iter(examples.values()))
        if isinstance(first, dict) and "value" in first:
            return first["value"]
    schema = param.get("schema")
    if isinstance(schema, dict):
        if "example" in schema:
            return schema["example"]
        if "default" in schema:
            return schema["default"]
        return _schema_placeholder(schema)
    return "1"


def _response_contract(
    document: dict[str, Any],
    operation: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    responses = operation.get("responses") or {}
    if not isinstance(responses, dict) or not responses:
        return 200, {}
    normalized_responses = {str(key): value for key, value in responses.items()}
    status_key = _preferred_status_key(normalized_responses)
    response = _resolve_ref(document, normalized_responses.get(status_key))
    schema: dict[str, Any] = {}
    if isinstance(response, dict):
        content = response.get("content") or {}
        if isinstance(content, dict):
            media = content.get("application/json") or next(iter(content.values()), {})
            if isinstance(media, dict):
                raw_schema = _resolve_schema_refs(document, media.get("schema"))
                if isinstance(raw_schema, dict):
                    schema = raw_schema
    return (200 if status_key == "default" else int(status_key)), schema


def _preferred_status_key(responses: dict[str, Any]) -> str:
    concrete = sorted(str(key) for key in responses if str(key).isdigit())
    for key in concrete:
        if 200 <= int(key) < 300:
            return key
    return concrete[0] if concrete else "default"


def _resolve_ref(document: dict[str, Any], value: Any) -> Any:
    if isinstance(value, dict) and "$ref" in value:
        ref = str(value.get("$ref") or "")
        if not ref.startswith("#/"):
            raise ValueError(f"unsupported external ref: {ref}")
        cursor: Any = document
        for part in ref.removeprefix("#/").split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(cursor, dict) or part not in cursor:
                raise ValueError(f"unresolved ref: {ref}")
            cursor = cursor[part]
        return cursor
    return value


def _resolve_schema_refs(
    document: dict[str, Any],
    value: Any,
    *,
    keep_examples: bool = False,
) -> Any:
    resolved = copy.deepcopy(_resolve_ref(document, value))
    if isinstance(resolved, dict):
        return {
            key: _resolve_schema_refs(
                document,
                nested,
                keep_examples=keep_examples,
            )
            for key, nested in resolved.items()
            if keep_examples or key not in {"description", "example", "examples"}
        }
    if isinstance(resolved, list):
        return [
            _resolve_schema_refs(document, item, keep_examples=keep_examples)
            for item in resolved
        ]
    return resolved


def _schema_placeholder(schema: dict[str, Any]) -> Any:
    schema_type = str(schema.get("type") or "string")
    if "enum" in schema and isinstance(schema["enum"], list) and schema["enum"]:
        return schema["enum"][0]
    return {
        "integer": 1,
        "number": 1,
        "boolean": True,
        "array": [],
        "object": {},
    }.get(schema_type, "1")


def _schema_example(schema: dict[str, Any]) -> Any:
    if "example" in schema:
        return schema["example"]
    if "default" in schema:
        return schema["default"]
    schema_type = str(schema.get("type") or "").strip()
    if schema_type == "object" or isinstance(schema.get("properties"), dict):
        properties = schema.get("properties") or {}
        if not isinstance(properties, dict):
            return {}
        required = {
            str(item)
            for item in schema.get("required", [])
            if str(item).strip()
        }
        keys = required or {str(key) for key in properties}
        return {
            str(key): _schema_example(value if isinstance(value, dict) else {})
            for key, value in properties.items()
            if str(key) in keys
        }
    if schema_type == "array":
        item_schema = schema.get("items")
        return [_schema_example(item_schema if isinstance(item_schema, dict) else {})]
    return _schema_placeholder(schema)


def _server_url(document: dict[str, Any]) -> str:
    servers = document.get("servers")
    if isinstance(servers, list) and servers:
        first = servers[0]
        if isinstance(first, dict):
            return str(first.get("url") or "")
    host = str(document.get("host") or "").strip()
    if host:
        scheme = "https"
        schemes = document.get("schemes")
        if isinstance(schemes, list) and schemes:
            scheme = str(schemes[0])
        return f"{scheme}://{host}{document.get('basePath') or ''}"
    return ""


def _normalize_base_url(value: str) -> str:
    return value.strip().rstrip("/")


def _join_url(base_url: str, request_path: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", request_path.lstrip("/"))
