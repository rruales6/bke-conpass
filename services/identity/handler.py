"""identity service — GET /me. Fully implemented (reads verified JWT claims)."""
from conpass_common import CurrentIdentity, create_app, lambda_handler
from conpass_common.models import Identity as IdentityModel

app = create_app(service="identity")


@app.get("/me")
def get_me(identity: CurrentIdentity):
    return IdentityModel.model_validate({
        "userId": identity.user_id,
        "email": identity.email,
        "roles": identity.roles,
        "merchantId": identity.merchant_id,
        "station": identity.station,
    }).model_dump(by_alias=True, exclude_none=True)


handler = lambda_handler(app)
