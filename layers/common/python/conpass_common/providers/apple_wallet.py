"""Apple Wallet provider — placeholder proving the abstraction is vendor-neutral.

Implemented in a later phase (PassKit .pkpass signing + APNs push updates + the
PassKit web-service protocol, BYO-cert for Enterprise). It requires no changes to any
feature Lambda — only this class and the apple_wallet.* secrets. Kept here so the
factory and data model (wallet_objects table) are Apple-ready from day one.
"""
from __future__ import annotations

from ..errors import NotConfigured
from .wallet import IssuedPass, PassContent, WalletKind, WalletProvider


class AppleWalletProvider(WalletProvider):
    kind = WalletKind.APPLE

    def _unconfigured(self):
        raise NotConfigured(
            "Apple Wallet integration is not implemented yet. When ready, add to "
            "secrets.yaml: apple_wallet.{pass_type_id, team_id, p12_path, p12_password, "
            "wwdr_cert_path} and complete AppleWalletProvider."
        )

    def issue(self, content: PassContent) -> IssuedPass:
        self._unconfigured()

    def update(self, content: PassContent) -> None:
        self._unconfigured()

    def revoke(self, provider_object_id: str) -> None:
        self._unconfigured()

    def add_link(self, content: PassContent) -> str:
        self._unconfigured()
