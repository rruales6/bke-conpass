"""admin service — platform-admin, cross-tenant (Función 04). Requires platform_admin."""
from conpass_common import CurrentIdentity, create_app, lambda_handler
from conpass_common.errors import NotImplementedYet
from conpass_common.models import SubscriptionUpdate

app = create_app(service="admin")


@app.get("/admin/clients")
def list_clients(identity: CurrentIdentity):
    identity.require_role("platform_admin")
    raise NotImplementedYet("Phase 6")


@app.patch("/admin/clients/{merchant_id}/subscription")
def update_subscription(merchant_id: str, body: SubscriptionUpdate, identity: CurrentIdentity):
    identity.require_role("platform_admin")
    raise NotImplementedYet("Phase 6")


@app.get("/admin/stats")
def get_platform_stats(identity: CurrentIdentity):
    identity.require_role("platform_admin")
    raise NotImplementedYet("Phase 6")


handler = lambda_handler(app)
