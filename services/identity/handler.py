"""identity service — GET /me (reads verified JWT claims) and POST /me/password
(first-login password change for emailed temporary passwords)."""
from conpass_common import CurrentIdentity, create_app, lambda_handler
from conpass_common.db import service_client
from conpass_common.demo import DEMO_OWNER_EMAIL
from conpass_common.errors import Forbidden
from conpass_common.locations import identity_locations
from conpass_common.logging import get_logger
from conpass_common.models import ChangePasswordRequest
from conpass_common.models import Identity as IdentityModel
from conpass_common.provisioning import complete_password_change

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


@app.get("/me")
def get_me(identity: CurrentIdentity):
    return IdentityModel.model_validate({
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


handler = lambda_handler(app)
