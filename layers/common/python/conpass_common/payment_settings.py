"""Shared row→API mapping for `platform_payment_settings` (Phase 10).

Both the merchants service (public GET /payment-settings, read by the signup page) and
the admin service (PATCH /admin/payment-settings, edited by platform-admin) render the
exact same PaymentSettings shape from the exact same singleton row. Living here — the
shared layer both Lambdas bundle — is the one place that can't drift between the two.
"""
from __future__ import annotations

from .assets import public_asset_url


def payment_settings_model(row: dict | None) -> dict:
    """Map a `platform_payment_settings` row to the PaymentSettings API shape.

    `configured` flips true once an account number is set — that's the signal the
    signup page uses to show transfer details vs. tell the visitor to contact conpass
    instead. A missing row (the migration seeds the singleton, so this shouldn't
    happen) degrades to `configured: False` rather than a 500.
    """
    if row is None:
        return {"configured": False}
    return {
        "configured": bool(row.get("account_number")),
        "bankName": row.get("bank_name"),
        "accountType": row.get("account_type"),
        "accountNumber": row.get("account_number"),
        "beneficiaryName": row.get("beneficiary_name"),
        "beneficiaryTaxId": row.get("beneficiary_tax_id"),
        "contactEmail": row.get("contact_email"),
        "instructions": row.get("instructions"),
        "qrImageUrl": public_asset_url(row.get("qr_storage_key")),
        "updatedAt": row.get("updated_at"),
    }
