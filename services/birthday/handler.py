"""birthday service — birthday benefit automation & issuance (Función 05E)."""
from typing import Annotated

from conpass_common import CurrentIdentity, create_app, lambda_handler
from conpass_common.db import service_client
from conpass_common.errors import NotImplementedYet
from conpass_common.models import BirthdayAutomation, BirthdayCardCreate
from conpass_common.plans import AUTOMATED_CAMPAIGNS, require_feature, tier_for_merchant
from fastapi import Header

app = create_app(service="birthday")


def _require_automated_campaigns(identity: CurrentIdentity) -> None:
    """Birthday automation is a Pro feature (402 feature_not_in_plan below it)."""
    if identity.merchant_id is not None:
        require_feature(tier_for_merchant(service_client(), identity.merchant_id),
                        AUTOMATED_CAMPAIGNS)


@app.put("/programs/{program_id}/birthday-automation")
def set_birthday_automation(program_id: str, body: BirthdayAutomation, identity: CurrentIdentity):
    identity.require_role("merchant_owner")
    _require_automated_campaigns(identity)
    raise NotImplementedYet("Phase 6")


@app.post("/birthday-cards", status_code=201)
def issue_birthday_card(
    body: BirthdayCardCreate,
    identity: CurrentIdentity,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    identity.require_role("merchant_owner", "operation_user")
    _require_automated_campaigns(identity)
    raise NotImplementedYet("Phase 6")


handler = lambda_handler(app)
