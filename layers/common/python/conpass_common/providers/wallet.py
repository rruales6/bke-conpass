"""Wallet provider abstraction.

Feature Lambdas depend ONLY on `WalletProvider` — never on a vendor SDK. Google is
implemented now; Apple slots in later with zero changes to feature code (D8, B2).
The backend is the authority for balances; a provider only reflects state into the
user's wallet (B1).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum

from ..assets import public_asset_url

DEFAULT_LANGUAGE = "es"
SUPPORTED_LANGUAGES = ("es", "en")


def normalize_language(value: str | None) -> str:
    """Coerce anything stored or sent to one of the languages a pass can render.

    The pass has no fallback chain of its own — an unknown tag would render empty
    labels — so everything funnels through here and lands on Ecuador-first Spanish.
    """
    tag = (value or "").strip().lower()
    for lang in SUPPORTED_LANGUAGES:
        if tag.startswith(lang):
            return lang
    return DEFAULT_LANGUAGE


class WalletKind(StrEnum):
    GOOGLE = "google"
    APPLE = "apple"


@dataclass(frozen=True)
class PassContent:
    """Vendor-neutral description of what a card shows. Providers translate this
    into their own object model (Google loyalty/generic object, Apple .pkpass)."""

    card_id: str
    program_id: str
    merchant_id: str
    program_type: str            # loyalty_stamps | loyalty_points | membership_pass
    program_name: str
    merchant_name: str
    holder_name: str | None
    opaque_token: str            # rendered as the QR
    # Display state (backend-authoritative):
    stamps: int = 0
    stamps_for_reward: int | None = None
    points: int = 0
    points_for_reward: int | None = None
    rewards_available: int = 0   # earned, waiting to be claimed
    rewards_redeemed: int = 0    # already claimed — a separate running total
    reward_text: str | None = None
    membership_active_until: str | None = None
    membership_includes: str | None = None
    # Holder identity shown on the pass. The card's opaque token stays the QR and is
    # never printed as text — a readable id would defeat the point of the token (B6).
    member_email: str | None = None
    # Label language, captured from the enrollment form (D16).
    language: str = DEFAULT_LANGUAGE
    # Appearance:
    accent_color: str | None = None
    logo_url: str | None = None
    background_url: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class IssuedPass:
    provider: WalletKind
    add_link: str                # "Add to <wallet>" URL
    provider_object_id: str | None = None
    provider_class_id: str | None = None


class WalletProvider(ABC):
    """One implementation per wallet vendor."""

    kind: WalletKind

    @abstractmethod
    def issue(self, content: PassContent) -> IssuedPass:
        """Create the pass object/class and return an add-to-wallet link."""

    @abstractmethod
    def update(self, content: PassContent) -> None:
        """Push new backend-authoritative state to an already-issued pass."""

    @abstractmethod
    def revoke(self, provider_object_id: str) -> None:
        """Expire/invalidate a pass."""

    @abstractmethod
    def add_link(self, content: PassContent) -> str:
        """Return (or regenerate) the add-to-wallet link without mutating state."""

    @abstractmethod
    def sync_program(self, content: PassContent) -> None:
        """Refresh the vendor's PROGRAM-level template from the program's own state.

        Separate from `update` because it is per-program, not per-card: a program edit
        calls this once and `update` once per installed card.
        """


def build_pass_content(
    card: dict, program: dict, merchant: dict | None, customer: dict | None = None
) -> PassContent:
    """Map canonical DB rows (cards/programs/merchants/customers) → a vendor-neutral
    PassContent.

    Shared by enrollment, cards, operations and programs so the row→pass mapping lives in
    one place. Balances come from the CARD (backend-authoritative); labels/appearance from
    the PROGRAM; the issuer name from the MERCHANT; the contact line from the CUSTOMER,
    which is optional — an anonymous enrollment stores no email (B6).
    """
    mu = card.get("membership_active_until")
    return PassContent(
        card_id=str(card["id"]),
        program_id=str(program["id"]),
        merchant_id=str(card["merchant_id"]),
        program_type=program["type"],
        program_name=program["name"],
        merchant_name=(merchant or {}).get("business_name") or "",
        holder_name=card.get("holder_name"),
        opaque_token=card["opaque_token"],
        stamps=card.get("stamps") or 0,
        stamps_for_reward=program.get("stamps_for_reward"),
        points=card.get("points") or 0,
        points_for_reward=program.get("points_for_reward"),
        rewards_available=card.get("rewards_available") or 0,
        rewards_redeemed=card.get("rewards_redeemed") or 0,
        reward_text=program.get("reward"),
        membership_active_until=mu.isoformat() if hasattr(mu, "isoformat") else mu,
        membership_includes=program.get("membership_includes"),
        member_email=(customer or {}).get("email"),
        language=normalize_language(card.get("language")),
        accent_color=program.get("color"),
        logo_url=public_asset_url(program.get("icon_storage_key")),
        background_url=public_asset_url(program.get("background_storage_key")),
    )
