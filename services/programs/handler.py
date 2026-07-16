"""programs service — program config & metrics (Función 05)."""
from __future__ import annotations

from conpass_common import CurrentIdentity, create_app, lambda_handler
from conpass_common.errors import Forbidden, NotFound, NotImplementedYet, TierLimit
from conpass_common.models import ProgramCreate, ProgramUpdate

from .repository import ProgramsRepository

ENROLL_BASE = "https://conpass.cards/enroll"

app = create_app(service="programs")


def get_repo() -> ProgramsRepository:
    return ProgramsRepository()


def _program_model(p: dict) -> dict:
    return {
        "id": p["id"], "merchantId": p["merchant_id"], "type": p["type"],
        "name": p["name"], "mechanic": p.get("mechanic"),
        "stampsForReward": p.get("stamps_for_reward"),
        "pointsForReward": p.get("points_for_reward"),
        "pointsPerDollar": (float(p["points_per_dollar"])
                            if p.get("points_per_dollar") is not None else None),
        "reward": p.get("reward"),
        "membershipValidity": p.get("membership_validity"),
        "membershipIncludes": p.get("membership_includes"),
        "welcomeBonus": p.get("welcome_bonus", 0),
        "expiryDays": p.get("expiry_days"),
        "appearance": {
            "color": p.get("color"),
            "iconStorageKey": p.get("icon_storage_key"),
            "backgroundStorageKey": p.get("background_storage_key"),
        },
        "wallets": p.get("wallets") or [],
        "active": p.get("active", True),
        "enrollmentUrl": f"{ENROLL_BASE}/{p['id']}",
        "createdAt": p["created_at"],
    }


@app.get("/programs")
def list_programs(identity: CurrentIdentity, merchantId: str | None = None):
    merchant_id = merchantId or identity.merchant_id
    if merchant_id is None:
        raise NotFound("no merchant in scope")
    identity.require_merchant(merchant_id)
    return [_program_model(p) for p in get_repo().list(merchant_id)]


@app.post("/programs", status_code=201)
def create_program(body: ProgramCreate, identity: CurrentIdentity):
    identity.require_role("merchant_owner")
    merchant_id = identity.merchant_id
    if merchant_id is None:
        raise Forbidden("no merchant in scope")
    repo = get_repo()
    limit = repo.program_limit(merchant_id)
    if limit is not None and repo.count_programs(merchant_id) >= limit:
        raise TierLimit(f"program limit ({limit}) reached for this tier")

    ap = body.appearance
    program = repo.create({
        "merchant_id": merchant_id,
        "type": body.type.value,
        "name": body.name,
        "mechanic": body.mechanic.value if body.mechanic else None,
        "stamps_for_reward": body.stampsForReward,
        "points_for_reward": body.pointsForReward,
        "points_per_dollar": body.pointsPerDollar,
        "reward": body.reward,
        "membership_validity": body.membershipValidity.value if body.membershipValidity else None,
        "membership_includes": body.membershipIncludes,
        "welcome_bonus": body.welcomeBonus or 0,
        "expiry_days": body.expiryDays,
        "color": ap.color if ap else None,
        "icon_storage_key": ap.iconStorageKey if ap else None,
        "background_storage_key": ap.backgroundStorageKey if ap else None,
        "wallets": [w.value for w in body.wallets],
    })
    return _program_model(program)


@app.get("/programs/{program_id}")
def get_program(program_id: str, identity: CurrentIdentity):
    p = get_repo().get(program_id)
    if p is None:
        raise NotFound("program not found")
    identity.require_merchant(p["merchant_id"])
    return _program_model(p)


@app.patch("/programs/{program_id}")
def update_program(program_id: str, body: ProgramUpdate, identity: CurrentIdentity):
    identity.require_role("merchant_owner")
    repo = get_repo()
    p = repo.get(program_id)
    if p is None:
        raise NotFound("program not found")
    identity.require_merchant(p["merchant_id"])

    patch: dict = {}
    if body.name is not None:
        patch["name"] = body.name
    if body.reward is not None:
        patch["reward"] = body.reward
    if body.stampsForReward is not None:
        patch["stamps_for_reward"] = body.stampsForReward
    if body.pointsForReward is not None:
        patch["points_for_reward"] = body.pointsForReward
    if body.pointsPerDollar is not None:
        patch["points_per_dollar"] = body.pointsPerDollar
    if body.active is not None:
        patch["active"] = body.active
    if body.appearance is not None:
        ap = body.appearance
        if ap.color is not None:
            patch["color"] = ap.color
        if ap.iconStorageKey is not None:
            patch["icon_storage_key"] = ap.iconStorageKey
        if ap.backgroundStorageKey is not None:
            patch["background_storage_key"] = ap.backgroundStorageKey
    updated = repo.update(program_id, patch) if patch else p
    return _program_model(updated)


@app.get("/programs/{program_id}/metrics")
def get_program_metrics(program_id: str, identity: CurrentIdentity):
    p = get_repo().get(program_id)
    if p is None:
        raise NotFound("program not found")
    identity.require_merchant(p["merchant_id"])
    # Aggregate metrics query lands in Phase 6 (second-visit rate, installs, churn).
    raise NotImplementedYet("Phase 6")


handler = lambda_handler(app)
