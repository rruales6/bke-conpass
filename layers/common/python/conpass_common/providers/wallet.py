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
    reward_text: str | None = None
    membership_active_until: str | None = None
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
