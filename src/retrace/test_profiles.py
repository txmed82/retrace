from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


SECRET_PROFILE_KEYS = {"password", "jwt", "token", "headers", "headers_json"}


@dataclass(frozen=True)
class ResolvedAuthProfile:
    name: str
    mode: str
    auth: dict[str, Any] = field(default_factory=dict)
    setup_steps: list[dict[str, Any]] = field(default_factory=list)
    browser_settings: dict[str, Any] = field(default_factory=dict)
    redacted_preview: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedEnvProfile:
    name: str
    api_base_url: str = ""
    app_url: str = ""
    headers_env: str = ""
    env_overrides: dict[str, str] = field(default_factory=dict)
    redacted_preview: dict[str, Any] = field(default_factory=dict)


def resolve_auth_profile(defaults: dict[str, Any], name: str) -> ResolvedAuthProfile:
    clean_name = name.strip()
    if not clean_name:
        return ResolvedAuthProfile(name="", mode="none")
    profiles = defaults.get("auth_profiles") or {}
    if not isinstance(profiles, dict) or clean_name not in profiles:
        raise ValueError(f"unknown auth profile: {clean_name}")
    profile = profiles.get(clean_name) or {}
    if not isinstance(profile, dict):
        raise ValueError(f"auth profile must be an object: {clean_name}")
    leaked = _secret_key_paths(profile)
    if leaked:
        raise ValueError(
            "auth profile must reference env vars, not secret values: "
            + ", ".join(sorted(leaked))
        )
    mode = str(profile.get("mode") or "headers").strip().lower()
    if mode not in {"none", "form", "jwt", "headers"}:
        raise ValueError(f"unsupported auth profile mode: {mode}")
    setup_steps = profile.get("auth_setup_steps", profile.get("setup_steps", []))
    if setup_steps in (None, ""):
        setup_steps = []
    if not isinstance(setup_steps, list) or not all(
        isinstance(step, dict) for step in setup_steps
    ):
        raise ValueError("auth profile setup_steps must be a list of objects")
    browser_settings = profile.get("browser_settings") or {}
    if not isinstance(browser_settings, dict):
        raise ValueError("auth profile browser_settings must be an object")
    auth: dict[str, Any] = {}
    if mode == "jwt":
        jwt_env = str(profile.get("jwt_env") or "").strip()
        if not jwt_env:
            raise ValueError("jwt auth profile requires jwt_env")
        auth = {"type": "bearer", "token_env": jwt_env}
    elif mode == "headers":
        headers_env = str(profile.get("headers_env") or "").strip()
        if not headers_env:
            raise ValueError("headers auth profile requires headers_env")
        auth = {"type": "headers", "headers_env": headers_env}
    return ResolvedAuthProfile(
        name=clean_name,
        mode=mode,
        auth=auth,
        setup_steps=[dict(step) for step in setup_steps],
        browser_settings=dict(browser_settings),
        redacted_preview=_redacted_profile_preview(profile),
    )


def resolve_env_profile(defaults: dict[str, Any], name: str) -> ResolvedEnvProfile:
    clean_name = name.strip()
    if not clean_name:
        return ResolvedEnvProfile(name="")
    profiles = defaults.get("env_profiles") or {}
    if not isinstance(profiles, dict) or clean_name not in profiles:
        raise ValueError(f"unknown env profile: {clean_name}")
    profile = profiles.get(clean_name) or {}
    if not isinstance(profile, dict):
        raise ValueError(f"env profile must be an object: {clean_name}")
    env_overrides = profile.get("env_overrides") or {}
    if not isinstance(env_overrides, dict):
        raise ValueError("env profile env_overrides must be an object")
    return ResolvedEnvProfile(
        name=clean_name,
        api_base_url=str(profile.get("api_base_url") or "").strip(),
        app_url=str(profile.get("app_url") or "").strip(),
        headers_env=str(profile.get("headers_env") or "").strip(),
        env_overrides={str(k): str(v) for k, v in dict(env_overrides).items()},
        redacted_preview=_redacted_profile_preview(profile),
    )


def validate_profiles(defaults: dict[str, Any]) -> dict[str, Any]:
    auth_profiles = defaults.get("auth_profiles") or {}
    env_profiles = defaults.get("env_profiles") or {}
    if auth_profiles and not isinstance(auth_profiles, dict):
        raise ValueError("tester.auth_profiles must be an object")
    if env_profiles and not isinstance(env_profiles, dict):
        raise ValueError("tester.env_profiles must be an object")
    auth = [
        resolve_auth_profile(defaults, str(name)).redacted_preview
        for name in sorted(auth_profiles)
    ]
    env = [
        resolve_env_profile(defaults, str(name)).redacted_preview
        for name in sorted(env_profiles)
    ]
    return {"auth_profiles": auth, "env_profiles": env}


def apply_api_profiles(
    spec: Any,
    *,
    defaults: dict[str, Any],
    auth_profile_name: str = "",
    env_profile_name: str = "",
) -> Any:
    auth_name = auth_profile_name.strip() or str(getattr(spec, "auth_profile", "") or "")
    env_name = env_profile_name.strip() or str(getattr(spec, "env_profile", "") or "")
    if auth_name:
        auth_profile = resolve_auth_profile(defaults, auth_name)
        if auth_profile.mode == "form":
            raise ValueError("API tests do not support form auth profiles")
        spec.auth = dict(auth_profile.auth)
        spec.auth_profile = auth_name
    if env_name:
        env_profile = resolve_env_profile(defaults, env_name)
        spec.env_overrides = {
            **env_profile.env_overrides,
            **{str(k): str(v) for k, v in dict(spec.env_overrides or {}).items()},
        }
        if env_profile.headers_env:
            spec.headers_env = env_profile.headers_env
        if env_profile.api_base_url:
            spec.url = _with_api_base_url(env_profile.api_base_url, str(spec.url))
            steps = []
            for step in list(getattr(spec, "steps", []) or []):
                item = dict(step)
                if item.get("url"):
                    item["url"] = _with_api_base_url(env_profile.api_base_url, str(item["url"]))
                steps.append(item)
            spec.steps = steps
        spec.env_profile = env_name
    return spec


def _with_api_base_url(base_url: str, url: str) -> str:
    if not base_url.strip() or not url.startswith("/"):
        return url
    return base_url.rstrip("/") + "/" + url.lstrip("/")


def _secret_key_paths(value: Any, path: str = "") -> list[str]:
    leaked: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            key_s = str(key)
            child = f"{path}.{key_s}" if path else key_s
            if key_s in SECRET_PROFILE_KEYS:
                leaked.append(child)
            leaked.extend(_secret_key_paths(nested, child))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            leaked.extend(_secret_key_paths(item, f"{path}[{idx}]"))
    return leaked


def _redacted_profile_preview(profile: dict[str, Any]) -> dict[str, Any]:
    def redact(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): (
                    "[secret-env]"
                    if str(key).endswith("_env")
                    else _redact_env_overrides(nested)
                    if str(key) == "env_overrides"
                    else redact(nested)
                )
                for key, nested in value.items()
                if str(key) not in SECRET_PROFILE_KEYS
            }
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    # Copy through JSON to keep previews stable and serializable.
    return json.loads(json.dumps(redact(profile), sort_keys=True))


def _redact_env_overrides(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): "[secret-env]" for key in value}
    return "[secret-env]"
