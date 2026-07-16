"""birthday service — birthday benefit automation & issuance (Función 05E)."""
from typing import Annotated

from conpass_common import CurrentIdentity, create_app, lambda_handler
from conpass_common.errors import NotImplementedYet
from conpass_common.models import BirthdayAutomation, BirthdayCardCreate
from fastapi import Header

app = create_app(service="birthday")


@app.put("/programs/{program_id}/birthday-automation")
def set_birthday_automation(program_id: str, body: BirthdayAutomation, identity: CurrentIdentity):
    identity.require_role("merchant_owner")
    raise NotImplementedYet("Phase 6")


@app.post("/birthday-cards", status_code=201)
def issue_birthday_card(
    body: BirthdayCardCreate,
    identity: CurrentIdentity,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    identity.require_role("merchant_owner", "operation_user")
    raise NotImplementedYet("Phase 6")


handler = lambda_handler(app)
