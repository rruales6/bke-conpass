"""notifications service — push/WhatsApp reminders (stubbed provider, D10)."""
from conpass_common import CurrentIdentity, create_app, lambda_handler
from conpass_common.db import service_client
from conpass_common.errors import NotImplementedYet
from conpass_common.models import ReminderRequest, Segment
from conpass_common.plans import (
    AUTOMATED_CAMPAIGNS,
    MANUAL_CAMPAIGNS,
    require_feature,
    tier_for_merchant,
)

app = create_app(service="notifications")


@app.post("/notifications/reminders", status_code=202)
def send_reminder(body: ReminderRequest, identity: CurrentIdentity):
    identity.require_role("merchant_owner")
    if identity.merchant_id is not None:
        tier = tier_for_merchant(service_client(), identity.merchant_id)
        require_feature(tier, MANUAL_CAMPAIGNS)
        if body.segment == Segment.birthday:
            require_feature(tier, AUTOMATED_CAMPAIGNS)
    # Phase 6: resolve eligible recipients, then get_notification_provider().send_bulk(...).
    raise NotImplementedYet("Phase 6")


handler = lambda_handler(app)
