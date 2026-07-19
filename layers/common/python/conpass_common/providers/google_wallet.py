"""Google Wallet provider — single-issuer model (B2, D8).

Implements the `WalletProvider` interface against the Google Wallet REST API using
**Generic** passes (one class per program, one object per card). The backend is the
authority for balances; this provider only reflects that state into the wallet:
`issue` creates/refreshes the object and returns a signed "Add to Google Wallet" save
link, `update` pushes new balances to an already-installed pass, `revoke` deactivates it.

Zero vendor SDKs: the service-account OAuth token and the save-link are both minted with
`PyJWT` (RS256), and REST calls use `httpx` — both already ship in the shared layer, so
no Lambda pays for `google-auth`.
"""
from __future__ import annotations

import json
import re
import time

import httpx
import jwt

from ..config import settings
from ..errors import NotConfigured
from .wallet import IssuedPass, PassContent, WalletKind, WalletProvider

GOOGLE_WALLET_SCOPE = "https://www.googleapis.com/auth/wallet_object.issuer"
SAVE_LINK_BASE = "https://pay.google.com/gp/v/save/"
API_BASE = "https://walletobjects.googleapis.com/walletobjects/v1"
_TOKEN_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_HTTP_TIMEOUT = 10.0
_LANG = "es"  # Ecuador-first; providers render a single default locale.


def _loc(value: str) -> dict:
    """A Google `LocalizedString` with an ES default."""
    return {"defaultValue": {"language": _LANG, "value": value}}


class GoogleWalletProvider(WalletProvider):
    kind = WalletKind.GOOGLE

    def __init__(self) -> None:
        self._issuer_id = settings.google_wallet_issuer_id
        self._sa: dict | None = None
        self._token: tuple[str, float] | None = None  # (access_token, expiry_epoch)

    # --- credential loading -------------------------------------------------
    def _credentials(self) -> dict:
        if self._sa is not None:
            return self._sa
        content = settings.google_wallet_sa_content
        if not self._issuer_id or not content:
            raise NotConfigured(
                "Google Wallet is not configured. Add to secrets.yaml:\n"
                "  google_wallet:\n"
                "    issuer_id: <your Google Wallet issuer id>\n"
                "    service_account_json: <path to service-account .json, "
                "relative to the secrets dir>"
            )
        try:
            self._sa = json.loads(content)
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise NotConfigured(f"google_wallet service-account JSON is invalid: {exc}") from exc
        return self._sa

    def _access_token(self) -> str:
        """Mint (and briefly cache) an OAuth2 access token from the service account."""
        now = time.time()
        if self._token and self._token[1] - 60 > now:
            return self._token[0]
        sa = self._credentials()
        claims = {
            "iss": sa["client_email"],
            "scope": GOOGLE_WALLET_SCOPE,
            "aud": sa["token_uri"],
            "iat": int(now),
            "exp": int(now) + 3600,
        }
        assertion = jwt.encode(claims, sa["private_key"], algorithm="RS256")
        resp = httpx.post(
            sa["token_uri"],
            data={"grant_type": _TOKEN_GRANT, "assertion": assertion},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = (payload["access_token"], now + payload.get("expires_in", 3600))
        return self._token[0]

    def _api(self) -> httpx.Client:
        return httpx.Client(
            base_url=API_BASE,
            headers={"Authorization": f"Bearer {self._access_token()}"},
            timeout=_HTTP_TIMEOUT,
        )

    # --- id / payload mapping ----------------------------------------------
    def _class_id(self, content: PassContent) -> str:
        # One class per program under the shared issuer (single-issuer model).
        return f"{self._issuer_id}.program_{content.program_id}"

    def _object_id(self, content: PassContent) -> str:
        return f"{self._issuer_id}.card_{content.card_id}"

    def _class_payload(self, content: PassContent) -> dict:
        return {
            "id": self._class_id(content),
            "issuerName": content.merchant_name,
            "reviewStatus": "UNDER_REVIEW",
            "multipleDevicesAndHoldersAllowedStatus": "ONE_USER_ALL_DEVICES",
        }

    def _text_modules(self, content: PassContent) -> list[dict]:
        mods: list[dict] = []
        if content.program_type == "loyalty_stamps":
            total = content.stamps_for_reward
            body = f"{content.stamps}" + (f" / {total}" if total else "")
            mods.append({"id": "balance", "header": "Sellos", "body": body})
        elif content.program_type == "loyalty_points":
            total = content.points_for_reward
            body = f"{content.points}" + (f" / {total}" if total else "")
            mods.append({"id": "balance", "header": "Puntos", "body": body})
        elif content.program_type == "membership_pass" and content.membership_active_until:
            mods.append(
                {"id": "validity", "header": "Válida hasta",
                 "body": content.membership_active_until}
            )
        if content.reward_text:
            mods.append({"id": "reward", "header": "Premio", "body": content.reward_text})
        return mods

    def _object_payload(self, content: PassContent) -> dict:
        obj: dict = {
            "id": self._object_id(content),
            "classId": self._class_id(content),
            "state": "ACTIVE",
            "cardTitle": _loc(content.merchant_name),
            "header": _loc(content.program_name),
            "barcode": {"type": "QR_CODE", "value": content.opaque_token},
            "textModulesData": self._text_modules(content),
        }
        if content.holder_name:
            obj["subheader"] = _loc(content.holder_name)
        if content.accent_color and _HEX_RE.match(content.accent_color):
            obj["hexBackgroundColor"] = content.accent_color
        if content.logo_url:
            obj["logo"] = {
                "sourceUri": {"uri": content.logo_url},
                "contentDescription": _loc(content.merchant_name),
            }
        return obj

    # --- WalletProvider interface -------------------------------------------
    def issue(self, content: PassContent) -> IssuedPass:
        obj = self._object_payload(content)
        object_id = obj["id"]
        with self._api() as api:
            self._ensure_class(api, content)
            existing = api.get(f"/genericObject/{object_id}")
            if existing.status_code == 404:
                api.post("/genericObject", json=obj).raise_for_status()
            else:
                existing.raise_for_status()
                api.put(f"/genericObject/{object_id}", json=obj).raise_for_status()
        return IssuedPass(
            provider=WalletKind.GOOGLE,
            add_link=self._save_link(object_id),
            provider_object_id=object_id,
            provider_class_id=obj["classId"],
        )

    def update(self, content: PassContent) -> None:
        obj = self._object_payload(content)
        patch = {"textModulesData": obj["textModulesData"], "state": obj["state"]}
        with self._api() as api:
            api.patch(f"/genericObject/{obj['id']}", json=patch).raise_for_status()

    def revoke(self, provider_object_id: str) -> None:
        with self._api() as api:
            api.patch(
                f"/genericObject/{provider_object_id}", json={"state": "INACTIVE"}
            ).raise_for_status()

    def add_link(self, content: PassContent) -> str:
        # No state mutation: reference the (already-issued) object by id.
        self._credentials()
        return self._save_link(self._object_id(content))

    # --- internals ----------------------------------------------------------
    def _ensure_class(self, api: httpx.Client, content: PassContent) -> None:
        class_id = self._class_id(content)
        found = api.get(f"/genericClass/{class_id}")
        if found.status_code == 404:
            api.post("/genericClass", json=self._class_payload(content)).raise_for_status()
        else:
            found.raise_for_status()

    def _save_link(self, object_id: str) -> str:
        sa = self._credentials()
        claims = {
            "iss": sa["client_email"],
            "aud": "google",
            "typ": "savetowallet",
            "iat": int(time.time()),
            "payload": {"genericObjects": [{"id": object_id}]},
        }
        token = jwt.encode(claims, sa["private_key"], algorithm="RS256")
        return SAVE_LINK_BASE + token
