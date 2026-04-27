from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from retrace.storage import SDKKeyRow, ServiceTokenRow, Storage


SDK_KEY_PREFIX = "rtpk"
SERVICE_TOKEN_PREFIX = "rtst"


@dataclass(frozen=True)
class CreatedSDKKey:
    id: str
    key: str
    prefix: str
    last4: str


@dataclass(frozen=True)
class CreatedServiceToken:
    id: str
    token: str
    prefix: str
    last4: str
    scopes: list[str]


def generate_sdk_key() -> str:
    return f"{SDK_KEY_PREFIX}_{secrets.token_urlsafe(32)}"


def generate_service_token() -> str:
    return f"{SERVICE_TOKEN_PREFIX}_{secrets.token_urlsafe(32)}"


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def create_sdk_key(
    store: Storage,
    *,
    project_id: str,
    environment_id: str,
    name: str,
) -> CreatedSDKKey:
    raw_key = generate_sdk_key()
    prefix = raw_key.split("_", 1)[0]
    last4 = raw_key[-4:]
    key_id = store.create_sdk_key(
        project_id=project_id,
        environment_id=environment_id,
        name=name,
        key_hash=hash_key(raw_key),
        prefix=prefix,
        last4=last4,
    )
    return CreatedSDKKey(id=key_id, key=raw_key, prefix=prefix, last4=last4)


def create_service_token(
    store: Storage,
    *,
    project_id: str,
    name: str,
    scopes: list[str],
) -> CreatedServiceToken:
    raw_token = generate_service_token()
    prefix = raw_token.split("_", 1)[0]
    last4 = raw_token[-4:]
    token_id = store.create_service_token(
        project_id=project_id,
        name=name,
        token_hash=hash_key(raw_token),
        scopes=scopes,
    )
    return CreatedServiceToken(
        id=token_id,
        token=raw_token,
        prefix=prefix,
        last4=last4,
        scopes=[str(s) for s in scopes],
    )


def authenticate_sdk_key(store: Storage, raw_key: str) -> SDKKeyRow | None:
    key = raw_key.strip()
    if not key.startswith(f"{SDK_KEY_PREFIX}_"):
        return None
    row = store.get_sdk_key_by_hash(hash_key(key))
    if row is None or row.revoked_at is not None:
        return None
    store.touch_sdk_key(row.id)
    return row


def authenticate_service_token(
    store: Storage, raw_token: str
) -> ServiceTokenRow | None:
    token = raw_token.strip()
    if not token.startswith(f"{SERVICE_TOKEN_PREFIX}_"):
        return None
    row = store.get_service_token_by_hash(hash_key(token))
    if row is None or row.revoked_at is not None:
        return None
    store.touch_service_token(row.id)
    return row
