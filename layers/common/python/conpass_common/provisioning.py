"""User provisioning via Supabase Auth admin API (Phase 2).

Creates an auth user whose `app_metadata` carries the app roles + tenant (so the JWT the
backend verifies already contains them — see conpass_common.auth), and a matching
`profiles` row. Returns a temporary password for first login (the design's "contraseña
temporal", Función 03); owners receive it by email and must change it at first login
(`app_metadata.must_change_password`). Uses the SECRET key (service_role) — backend only.

Every admin write of `app_metadata` sends the FULL merged dict (read-merge-write): we
have not proven whether the server merges or replaces, so never send a partial patch.
"""
from __future__ import annotations

import secrets
import string
from dataclasses import dataclass

from .db import service_client
from .errors import Conflict, ValidationFailed
from .logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class ProvisionResult:
    user_id: str
    email: str
    temp_password: str


def _temp_password() -> str:
    # 15 chars, url-safe, always containing a digit so a stricter Supabase password
    # policy (letters + digits) still accepts it. Delivered once, changed on first login.
    return secrets.token_urlsafe(10) + secrets.choice(string.digits)


def _merged_app_metadata(client, user_id: str, patch: dict) -> dict:
    """Current app_metadata of `user_id` merged with `patch` (the full dict to write)."""
    res = client.auth.admin.get_user_by_id(user_id)
    current = getattr(getattr(res, "user", None), "app_metadata", None)
    if not isinstance(current, dict) or not current.get("roles"):
        # Writing `patch` alone would strip roles / merchant_id from the login.
        raise Conflict(f"could not read the current login metadata for {user_id}")
    return {**current, **patch}


def provision_user(
    *,
    email: str,
    roles: list[str],
    merchant_id: str | None = None,
    station: str | None = None,
    name: str | None = None,
    must_change_password: bool = False,
    language: str | None = None,
) -> ProvisionResult:
    """Create the auth user + profile. Raises Conflict if the email already exists."""
    client = service_client()
    password = _temp_password()

    app_metadata: dict = {"roles": roles}
    if merchant_id:
        app_metadata["merchant_id"] = merchant_id
    if station:
        app_metadata["station"] = station
    if must_change_password:
        app_metadata["must_change_password"] = True

    attrs: dict = {
        "email": email,
        "password": password,
        "email_confirm": True,          # usable immediately; no email round-trip needed
        "app_metadata": app_metadata,
    }
    if language:
        attrs["user_metadata"] = {"language": language}

    try:
        res = client.auth.admin.create_user(attrs)
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
    if auth_attrs or station is not None:
        try:
            if station is not None:
                auth_attrs["app_metadata"] = _merged_app_metadata(
                    client, user_id, {"station": station})
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


def issue_temp_password(user_id: str) -> str:
    """New temporary password + `must_change_password` = True in ONE admin write
    (roles / merchant_id preserved by the merge). Returns the password once."""
    client = service_client()
    password = _temp_password()
    try:
        client.auth.admin.update_user_by_id(user_id, {
            "password": password,
            "app_metadata": _merged_app_metadata(
                client, user_id, {"must_change_password": True}),
        })
    except Exception as exc:  # noqa: BLE001
        raise Conflict(f"could not reset password for {user_id}") from exc
    log.info("issued_temp_password", extra={"user_id": user_id})
    return password


def is_current_password(email: str, password: str) -> bool:
    """True when `password` still signs `email` in, i.e. it is the CURRENT password.

    Supabase's admin update accepts the same password again, so this is the only way to
    stop an owner "changing" the emailed temporary password to itself. Probes the public
    password grant with the publishable key; any failure other than a clean 200 counts
    as "not current" (an infra hiccup must not block a legitimate change)."""
    import httpx

    from .config import settings

    if not (settings.supabase_url and settings.supabase_publishable_key):
        return False
    try:
        resp = httpx.post(f"{settings.supabase_url}/auth/v1/token?grant_type=password",
                          headers={"apikey": settings.supabase_publishable_key},
                          json={"email": email, "password": password}, timeout=5)
    except httpx.HTTPError:
        log.warning("current_password_probe_failed", extra={"email": email})
        return False
    return resp.status_code == 200


def complete_password_change(user_id: str, new_password: str, *, email: str | None = None) -> None:
    """Set the user's chosen password and clear `must_change_password` in ONE write.
    With `email`, refuses a new password equal to the current one (`same_password`)."""
    from supabase_auth.errors import AuthApiError, AuthError, AuthWeakPasswordError

    if email and is_current_password(email, new_password):
        raise ValidationFailed("new password must differ from the current one",
                               details={"reason": "same_password"})
    client = service_client()
    try:
        client.auth.admin.update_user_by_id(user_id, {
            "password": new_password,
            "app_metadata": _merged_app_metadata(
                client, user_id, {"must_change_password": False}),
        })
    except AuthWeakPasswordError as exc:
        raise ValidationFailed(
            "password too weak: choose a longer or more varied one",
            details={"reason": "weak_password", "reasons": list(exc.reasons or [])}) from exc
    except AuthApiError as exc:
        if exc.code == "weak_password":
            raise ValidationFailed(
                "password too weak: choose a longer or more varied one",
                details={"reason": "weak_password",
                         "reasons": list(getattr(exc, "reasons", None) or [])}) from exc
        if exc.code == "same_password":
            raise ValidationFailed("new password must differ from the current one",
                                   details={"reason": "same_password"}) from exc
        log.warning("password_change_auth_error",
                    extra={"user_id": user_id, "auth_code": exc.code, "status": exc.status})
        raise Conflict(f"could not change password for {user_id}") from exc
    except AuthError as exc:
        log.warning("password_change_auth_error",
                    extra={"user_id": user_id, "auth_code": getattr(exc, "code", None)})
        raise Conflict(f"could not change password for {user_id}") from exc
    log.info("password_changed", extra={"user_id": user_id})


def get_user_language(user_id: str) -> str:
    """The language chosen at signup (user_metadata.language), "es" when absent."""
    res = service_client().auth.admin.get_user_by_id(user_id)
    meta = getattr(getattr(res, "user", None), "user_metadata", None)
    lang = meta.get("language") if isinstance(meta, dict) else None
    return lang if lang in ("es", "en") else "es"


def delete_user(user_id: str) -> None:
    """Best-effort teardown (used for onboarding rollback + tests)."""
    try:
        service_client().auth.admin.delete_user(user_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("delete_user_failed", extra={"user_id": user_id, "err": str(exc)})
