"""User provisioning via Supabase Auth admin API (Phase 2).

Creates an auth user whose `app_metadata` carries the app roles + tenant (so the JWT the
backend verifies already contains them — see conpass_common.auth), and a matching
`profiles` row. Returns a temporary password for first login (the design's "contraseña
temporal", Función 03). Uses the SECRET key (service_role) — backend only.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from .db import service_client
from .errors import Conflict
from .logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class ProvisionResult:
    user_id: str
    email: str
    temp_password: str


def _temp_password() -> str:
    # ~14 chars, url-safe; meets Supabase's default policy. Shown once, changed on login.
    return secrets.token_urlsafe(10)


def provision_user(
    *,
    email: str,
    roles: list[str],
    merchant_id: str | None = None,
    station: str | None = None,
    name: str | None = None,
) -> ProvisionResult:
    """Create the auth user + profile. Raises Conflict if the email already exists."""
    client = service_client()
    password = _temp_password()

    app_metadata: dict = {"roles": roles}
    if merchant_id:
        app_metadata["merchant_id"] = merchant_id
    if station:
        app_metadata["station"] = station

    try:
        res = client.auth.admin.create_user({
            "email": email,
            "password": password,
            "email_confirm": True,          # usable immediately; no email round-trip needed
            "app_metadata": app_metadata,
        })
    except Exception as exc:  # noqa: BLE001 — normalize to a typed error
        raise Conflict(f"could not create login for {email}: {exc}") from exc

    user_id = res.user.id
    client.table("profiles").insert({
        "user_id": user_id,
        "email": email,
        "merchant_id": merchant_id,
        "role": roles[0],
        "station": station,
        "name": name,
    }).execute()

    log.info("provisioned_user", extra={"user_id": user_id, "roles": roles,
                                       "merchant_id": merchant_id})
    return ProvisionResult(user_id=user_id, email=email, temp_password=password)


def update_user(
    user_id: str,
    *,
    email: str | None = None,
    station: str | None = None,
    name: str | None = None,
) -> None:
    """Edit an existing auth user + profile (name / email / station).

    Keeps the auth user's `app_metadata.station` in sync so the JWT the backend
    verifies stays authoritative for cashier routing.
    """
    client = service_client()

    auth_attrs: dict = {}
    if email is not None:
        auth_attrs["email"] = email
    if station is not None:
        auth_attrs["app_metadata"] = {"station": station}
    if auth_attrs:
        try:
            client.auth.admin.update_user_by_id(user_id, auth_attrs)
        except Exception as exc:  # noqa: BLE001 — normalize to a typed error
            raise Conflict(f"could not update login for {user_id}: {exc}") from exc

    profile_patch: dict = {}
    if email is not None:
        profile_patch["email"] = email
    if station is not None:
        profile_patch["station"] = station
    if name is not None:
        profile_patch["name"] = name
    if profile_patch:
        client.table("profiles").update(profile_patch).eq("user_id", user_id).execute()

    log.info("updated_user", extra={"user_id": user_id})


def reset_password(user_id: str) -> str:
    """Issue a fresh temporary password for an existing user; returns it once."""
    password = _temp_password()
    try:
        service_client().auth.admin.update_user_by_id(user_id, {"password": password})
    except Exception as exc:  # noqa: BLE001
        raise Conflict(f"could not reset password for {user_id}: {exc}") from exc
    log.info("reset_password", extra={"user_id": user_id})
    return password


def delete_user(user_id: str) -> None:
    """Best-effort teardown (used for onboarding rollback + tests)."""
    try:
        service_client().auth.admin.delete_user(user_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("delete_user_failed", extra={"user_id": user_id, "err": str(exc)})
