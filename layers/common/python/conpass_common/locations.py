"""Locales (Phase 16) — shared helpers.

    active_locations(client, merchant_id) -> list[dict]      active rows, oldest first
    identity_locations(client, identity) -> list[dict]       [{id, name}] the user may scan at
    resolve_scan_location(client, identity, merchant_id, location_id) -> str | None
        None when location_id is None; else the id after checking it is an ACTIVE locale of
        `merchant_id` and (operation_user) assigned to the caller — ValidationFailed (422)
        otherwise.

Why shared: /me (what the cashier may pick), the operations service (where a scan is
attributed) and the merchants service all need the same "who may use which locale" rule,
so it lives in one place.
"""
from __future__ import annotations

from .errors import ValidationFailed


def active_locations(client, merchant_id: str) -> list[dict]:
    """Active locales of a merchant, oldest first (the order the wallet class uses)."""
    return (client.table("merchant_locations").select("*")
            .eq("merchant_id", merchant_id).eq("active", True)
            .order("created_at").execute().data or [])


def _assigned_location_ids(client, user_id: str) -> list[str]:
    rows = (client.table("operation_user_locations").select("location_id")
            .eq("user_id", user_id).execute().data or [])
    return [r["location_id"] for r in rows]


def _is_cashier(identity) -> bool:
    """A cashier is restricted to its assigned locales; an owner (even one who also holds
    the cashier role) is not."""
    return "operation_user" in identity.roles and "merchant_owner" not in identity.roles


def identity_locations(client, identity) -> list[dict]:
    """[{id, name}] the user may scan at: a cashier's assigned ACTIVE locales, every active
    locale for the owner, nothing for anyone else (admin, customer)."""
    merchant_id = identity.merchant_id
    if not merchant_id:
        return []
    if "merchant_owner" in identity.roles:
        rows = active_locations(client, merchant_id)
    elif "operation_user" in identity.roles:
        assigned = set(_assigned_location_ids(client, identity.user_id))
        rows = [r for r in active_locations(client, merchant_id) if r["id"] in assigned]
    else:
        return []
    return [{"id": r["id"], "name": r["name"]} for r in rows]


def _not_allowed(location_id: str) -> ValidationFailed:
    return ValidationFailed(
        "location is not an active locale you may scan at",
        details={"reason": "location_not_allowed", "locationId": location_id})


def resolve_scan_location(client, identity, merchant_id: str,
                          location_id: str | None) -> str | None:
    """Validate the locale a scan claims. None stays None (attribution is optional);
    anything else must be an active locale of `merchant_id`, and a cashier must also be
    assigned to it — otherwise a cashier could attribute visits to a store they don't work at."""
    if location_id is None:
        return None
    location_id = str(location_id)
    rows = (client.table("merchant_locations").select("id,active")
            .eq("id", location_id).eq("merchant_id", merchant_id).limit(1).execute().data or [])
    if not rows or not rows[0].get("active"):
        raise _not_allowed(location_id)
    if _is_cashier(identity) and location_id not in _assigned_location_ids(
            client, identity.user_id):
        raise _not_allowed(location_id)
    return location_id
