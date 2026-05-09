from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import yaml

from retrace.api_testing import APITestSpec, create_api_spec


OPENAPI_IMPORT_VERSION = 1
SUPPORTED_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


@dataclass
class OpenAPIImportResult:
    specs: list[APITestSpec]
    skipped: list[str]


def import_openapi_specs(
    *,
    openapi_path: Path,
    specs_dir: Path,
    base_url: str,
    path_filter: str = "",
    method_filter: str = "",
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
            if wanted_method and method_l != wanted_method:
                continue
            if not isinstance(operation, dict):
                skipped.append(f"{method_l.upper()} {raw_path}: operation is not an object")
                continue
            try:
                specs.append(
                    _operation_to_spec(
                        document=document,
                        specs_dir=specs_dir,
                        base_url=base,
                        raw_path=str(raw_path),
                        method=method_l.upper(),
                        path_parameters=inherited_parameters,
                        operation=operation,
                    )
                )
            except Exception as exc:
                skipped.append(f"{method_l.upper()} {raw_path}: {exc}")
    return OpenAPIImportResult(specs=specs, skipped=skipped)


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
) -> APITestSpec:
    operation_parameters = path_parameters + _parameters(document, operation.get("parameters"))
    request_path = _render_path(raw_path, operation_parameters)
    query = _query_defaults(operation_parameters)
    status, response_schema = _response_contract(document, operation)
    operation_id = str(operation.get("operationId") or "").strip()
    summary = str(operation.get("summary") or "").strip()
    name = summary or operation_id or f"{method} {raw_path}"
    return create_api_spec(
        specs_dir=specs_dir,
        name=f"OpenAPI {name}",
        method=method,
        url=_join_url(base_url, request_path),
        query=query,
        expected_status=status,
        schema_assertions=[{"schema": response_schema}] if response_schema else [],
        fixtures={
            "source": "openapi_import",
            "contract_derived": True,
            "openapi_import_version": OPENAPI_IMPORT_VERSION,
            "operation_id": operation_id,
            "openapi_path": raw_path,
            "openapi_method": method,
            "selected_response_status": str(status),
        },
    )


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
        return str(values.get(match.group(1)) or "1")

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
    status_key = _preferred_status_key(responses)
    response = _resolve_ref(document, responses.get(status_key))
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


def _resolve_schema_refs(document: dict[str, Any], value: Any) -> Any:
    resolved = copy.deepcopy(_resolve_ref(document, value))
    if isinstance(resolved, dict):
        return {
            key: _resolve_schema_refs(document, nested)
            for key, nested in resolved.items()
            if key not in {"description", "example", "examples"}
        }
    if isinstance(resolved, list):
        return [_resolve_schema_refs(document, item) for item in resolved]
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
