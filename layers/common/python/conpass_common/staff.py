"""Manager scope (Phase 18, D21): what an operation user with staff_role 'manager' may see.

The JWT's app_metadata.staff_role is only a hint (it can be up to an hour stale). Every grant of
manager powers goes through `manager_scope`, which reads `profiles.staff_role`, the merchant's
plan (`staff_roles` is Pro) and the assigned branches from the DB.
"""
from __future__ import annotations

from conpass_common.auth import Identity
from conpass_common.errors import Forbidden
from conpass_common.plans import STAFF_ROLES, get_plan, tier_for_merchant


def manager_scope(client, identity: Identity) -> list[str] | None:
    """Branch ids a caller is limited to.

    - None → no limit (merchant_owner, platform_admin).
    - list → a manager limited to these branch ids (possibly empty: no branch assigned yet).
    - Forbidden → a cashier, a manager whose merchant is no longer on Pro, or any other role.
    """
    if "platform_admin" in identity.roles or "merchant_owner" in identity.roles:
        return None
    if "operation_user" not in identity.roles or not identity.merchant_id:
        raise Forbidden("requires the owner or a manager")
    rows = (client.table("profiles").select("staff_role,merchant_id")
            .eq("user_id", identity.user_id).execute().data or [])
    if not rows or rows[0].get("staff_role") != "manager" \
            or rows[0].get("merchant_id") != identity.merchant_id:
        raise Forbidden("requires the owner or a manager")
    if not get_plan(tier_for_merchant(client, identity.merchant_id)).has(STAFF_ROLES):
        raise Forbidden("the manager role needs the Pro plan",
                        details={"reason": "manager_needs_pro"})
    links = (client.table("operation_user_locations").select("location_id")
             .eq("user_id", identity.user_id).execute().data or [])
    return [r["location_id"] for r in links]
