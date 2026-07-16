"""notifications service — push/WhatsApp reminders (stubbed provider, D10)."""
from conpass_common import CurrentIdentity, create_app, lambda_handler
from conpass_common.errors import NotImplementedYet
from conpass_common.models import ReminderRequest

app = create_app(service="notifications")


@app.post("/notifications/reminders", status_code=202)
def send_reminder(body: ReminderRequest, identity: CurrentIdentity):
    identity.require_role("merchant_owner")
    # Phase 6: resolve eligible recipients, then get_notification_provider().send_bulk(...).
    raise NotImplementedYet("Phase 6")


handler = lambda_handler(app)
