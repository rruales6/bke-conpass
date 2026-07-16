"""Google Wallet provider (single-issuer model, B2).

Full wiring lands in Phase 4 (needs google_wallet.* secrets). This module is the
concrete shape the Wallet specialist (Opus) will complete: it defines the class/object
mapping and the signed "Add to Google Wallet" JWT save-link, and raises NotConfigured
with an actionable message when credentials are absent — so everything upstream works
offline today.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import settings
from ..errors import NotConfigured
from .wallet import IssuedPass, PassContent, WalletKind, WalletProvider

GOOGLE_WALLET_SCOPE = "https://www.googleapis.com/auth/wallet_object.issuer"
SAVE_LINK_BASE = "https://pay.google.com/gp/v/save/"


class GoogleWalletProvider(WalletProvider):
    kind = WalletKind.GOOGLE

    def __init__(self) -> None:
        self._issuer_id = settings.google_wallet_issuer_id
        self._sa_ref = settings.google_wallet_service_account_json

    # --- credential loading -------------------------------------------------
    def _require_credentials(self) -> dict:
        if not self._issuer_id or not self._sa_ref:
            raise NotConfigured(
                "Google Wallet is not configured. Add to secrets.yaml:\n"
                "  google_wallet:\n"
                "    issuer_id: <your Google Wallet issuer id>\n"
                "    service_account_json: <path to service-account .json>"
            )
        ref = self._sa_ref.strip()
        if ref.startswith("{"):
            return json.loads(ref)
        p = Path(ref)
        if not p.exists():
            raise NotConfigured(f"google_wallet.service_account_json path not found: {ref}")
        return json.loads(p.read_text())

    def _class_id(self, content: PassContent) -> str:
        # One class per program under the shared issuer (single-issuer model).
        return f"{self._issuer_id}.program_{content.program_id}"

    def _object_id(self, content: PassContent) -> str:
        return f"{self._issuer_id}.card_{content.card_id}"

    # --- WalletProvider interface -------------------------------------------
    def issue(self, content: PassContent) -> IssuedPass:  # pragma: no cover - Phase 4
        self._require_credentials()
        raise NotImplementedError(
            "GoogleWalletProvider.issue is completed in Phase 4 (Wallet specialist). "
            "Mapping is defined: class=_class_id, object=_object_id, save-link via signed JWT."
        )

    def update(self, content: PassContent) -> None:  # pragma: no cover - Phase 4
        self._require_credentials()
        raise NotImplementedError("GoogleWalletProvider.update — Phase 4.")

    def revoke(self, provider_object_id: str) -> None:  # pragma: no cover - Phase 4
        self._require_credentials()
        raise NotImplementedError("GoogleWalletProvider.revoke — Phase 4.")

    def add_link(self, content: PassContent) -> str:  # pragma: no cover - Phase 4
        self._require_credentials()
        raise NotImplementedError("GoogleWalletProvider.add_link — Phase 4.")
