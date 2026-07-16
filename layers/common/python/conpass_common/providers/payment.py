"""Payment provider abstraction (stubbed per D9).

Real processors (DEUNA / Stripe / Kushki) drop in behind this interface later. For now
the manual-proof provider records the uploaded proof reference and marks the
subscription pending for platform-admin confirmation — no external calls.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class PaymentIntent:
    merchant_id: str
    tier: str
    amount_usd: float
    method: str                       # card | deuna | manual_transfer
    proof_storage_key: str | None = None


@dataclass(frozen=True)
class PaymentResult:
    status: str                       # pending | paid
    provider: str
    reference: str | None = None


class PaymentProvider(ABC):
    name: str

    @abstractmethod
    def submit(self, intent: PaymentIntent) -> PaymentResult:
        ...


class ManualProofPaymentProvider(PaymentProvider):
    """Stub: no processor. Records proof; platform-admin confirms payment."""

    name = "manual"

    def submit(self, intent: PaymentIntent) -> PaymentResult:
        # Nothing to charge — activation is confirmed by platform-admin (Función 04).
        return PaymentResult(status="pending", provider=self.name,
                             reference=intent.proof_storage_key)
