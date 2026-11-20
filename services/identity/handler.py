"""identity service — GET /me (reads verified JWT claims) and POST /me/password
(first-login password change for emailed temporary passwords)."""
from conpass_common import CurrentIdentity, create_app, lambda_handler
from conpass_common.db import service_client
from conpass_common.demo import DEMO_OWNER_EMAIL
from conpass_common.errors import Forbidden, ValidationFailed
from conpass_common.locations import identity_locations
from conpass_common.logging import get_logger
from conpass_common.models import ChangePasswordRequest, ForgotPasswordRequest
from conpass_common.models import Identity as IdentityModel
from conpass_common.password_reset import request_code, reset_with_code
from conpass_common.provisioning import complete_password_change
from fastapi import Response
from pydantic import BaseModel, ConfigDict, EmailStr

log = get_logger(__name__)
app = create_app(service="identity")


def _locations(identity: CurrentIdentity) -> list[dict]:
    """Locales the user may scan at. Only tenant users have any; a lookup failure degrades
    to none rather than blocking /me (and with it, login)."""
    if not ({"merchant_owner", "operation_user"} & set(identity.roles)):
        return []
    try:
        return identity_locations(service_client(), identity)
    except Exception as exc:  # noqa: BLE001
        log.warning("identity_locations_failed", extra={"error": type(exc).__name__})
        return []


def _profile_extras(identity: CurrentIdentity) -> dict:
    """Console-menu fields: display name, staff role, business name, plan tier. Two reads
    (profile + merchant embedded, then the subscription); a failure degrades to what the
    token already says, so /me — and with it login — never breaks."""
    extras: dict = {}
    local = (identity.email or "").split("@")[0]
    try:
        client = service_client()
        rows = (client.table("profiles").select("name,staff_role,merchants(business_name)")
                .eq("user_id", identity.user_id).execute().data or [])
        profile = rows[0] if rows else {}
        extras["name"] = (profile.get("name") or "").strip() or local
        if "operation_user" in identity.roles and profile.get("staff_role"):
            extras["staffRole"] = profile["staff_role"]
        merchant = profile.get("merchants")
        if isinstance(merchant, list):
            merchant = merchant[0] if merchant else None
        if merchant and merchant.get("business_name"):
            extras["businessName"] = merchant["business_name"]
        if identity.merchant_id and ({"merchant_owner", "operation_user"} & set(identity.roles)):
            subs = (client.table("subscriptions").select("tier")
                    .eq("merchant_id", identity.merchant_id).execute().data or [])
            if subs:
                extras["tier"] = subs[0]["tier"]
    except Exception as exc:  # noqa: BLE001
        log.warning("identity_extras_failed", extra={"error": type(exc).__name__})
        extras.setdefault("name", local)
    return extras


@app.get("/me")
def get_me(identity: CurrentIdentity):
    return IdentityModel.model_validate({
        **_profile_extras(identity),
        "userId": identity.user_id,
        "email": identity.email,
        "roles": identity.roles,
        "merchantId": identity.merchant_id,
        "station": identity.station,
        "locations": _locations(identity),
        "mustChangePassword": bool(identity.must_change_password),
    }).model_dump(by_alias=True, exclude_none=True)


@app.post("/me/password", status_code=204)
def change_password(body: ChangePasswordRequest, identity: CurrentIdentity):
    """Set a new password and clear `must_change_password`. The demo owner shares a
    public password, so it can never be changed."""
    if (identity.email or "").lower() == DEMO_OWNER_EMAIL.lower():
        raise Forbidden("the demo account password cannot be changed")
    complete_password_change(identity.user_id, body.newPassword, email=identity.email)
    return None


@app.post("/auth/forgot-password", status_code=202)
def forgot_password(body: ForgotPasswordRequest):
    """Public. 202 whether or not the address has an account; only the rate limit (429)
    can differ, and it counts every request, known address or not."""
    language = body.language.value if body.language is not None else None
    request_code(service_client(), str(body.email), language)
    return Response(status_code=202)


class _ResetBody(BaseModel):
    """Lenient on newPassword's length so a short one answers weak_password (the contract's
    422 reason) instead of a generic field error."""
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    code: str
    newPassword: str  # noqa: N815 - the contract's field name


@app.post("/auth/reset-password", status_code=204)
def reset_password_with_code(body: _ResetBody):
    """Public. The latest unused, unexpired code for the address is the only valid one."""
    if len(body.code) > 32 or len(body.newPassword) > 1024:
        raise ValidationFailed("invalid request", details={"reason": "invalid_code"})
    reset_with_code(service_client(), str(body.email), body.code, body.newPassword)
    return Response(status_code=204)


handler = lambda_handler(app)
