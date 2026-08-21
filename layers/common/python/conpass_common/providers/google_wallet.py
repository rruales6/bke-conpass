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
import logging
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
# A save link is handed to a browser as a URL, so the signed JWT has to stay addressable.
# Google's guidance for the URL form is ~1800 characters; past that the button should use
# the JS API instead. We log rather than truncate — a shortened pass would be worse.
SAVE_LINK_MAX_URL = 1800

log = logging.getLogger(__name__)


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

    def _status_line(self, content: PassContent) -> str:
        """The card's tracked status, worded for the pass's most prominent line.

        Google renders `header` as the pass title and only shows `textModulesData` once
        the holder expands the details, so the balance goes in `header` — the whole point
        of the pass is seeing "how many stamps do I have" at a glance.
        """
        if content.program_type == "loyalty_stamps":
            total = content.stamps_for_reward
            return (f"{content.stamps} / {total} sellos" if total
                    else f"{content.stamps} sellos")
        if content.program_type == "loyalty_points":
            total = content.points_for_reward
            return (f"{content.points} / {total} puntos" if total
                    else f"{content.points} puntos")
        if content.program_type == "membership_pass":
            return (f"Activa hasta {content.membership_active_until}"
                    if content.membership_active_until else "Membresía activa")
        return content.program_name

    def _text_modules(self, content: PassContent) -> list[dict]:
        """The details section, below the barcode. The balance is NOT repeated here —
        it is the pass title (see `_status_line`)."""
        mods: list[dict] = []
        if content.holder_name:
            mods.append({"id": "holder", "header": "Titular", "body": content.holder_name})
        if content.reward_text:
            mods.append({"id": "reward", "header": "Premio", "body": content.reward_text})
        if content.program_type == "membership_pass" and content.membership_active_until:
            mods.append(
                {"id": "validity", "header": "Válida hasta",
                 "body": content.membership_active_until}
            )
        return mods

    def _object_payload(self, content: PassContent) -> dict:
        obj: dict = {
            "id": self._object_id(content),
            "classId": self._class_id(content),
            "state": "ACTIVE",
            "cardTitle": _loc(content.merchant_name),
            # subheader is the small label above the title; header is the title itself —
            # so the programme names the card and the tracked balance is what stands out.
            "subheader": _loc(content.program_name),
            "header": _loc(self._status_line(content)),
            "barcode": {"type": "QR_CODE", "value": content.opaque_token},
            "textModulesData": self._text_modules(content),
        }
        if content.accent_color and _HEX_RE.match(content.accent_color):
            obj["hexBackgroundColor"] = content.accent_color
        # No contentDescription on either image: it costs ~200 characters of encoded
        # JWT, and the save link has to carry this whole object inside an 1800-character
        # URL. cardTitle/header already say the same thing in text.
        if content.logo_url:
            obj["logo"] = {"sourceUri": {"uri": content.logo_url}}
        if content.background_url:
            # Banner across the front of the pass (GenericObject.heroImage).
            obj["heroImage"] = {"sourceUri": {"uri": content.background_url}}
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
            add_link=self._save_link(content, object_exists=True),
            provider_object_id=object_id,
            provider_class_id=obj["classId"],
        )

    def update(self, content: PassContent) -> None:
        """Push the pass's full backend-authoritative state.

        A PUT (full replace), not a partial patch: appearance changes have to reach an
        already-installed pass too, and only a replace can *clear* an image the merchant
        removed — a PATCH leaves omitted fields alone. The payload is derived entirely
        from the DB rows, so replacing is the same operation `issue` performs on an
        object that already exists.
        """
        obj = self._object_payload(content)
        with self._api() as api:
            api.put(f"/genericObject/{obj['id']}", json=obj).raise_for_status()

    def revoke(self, provider_object_id: str) -> None:
        with self._api() as api:
            api.patch(
                f"/genericObject/{provider_object_id}", json={"state": "INACTIVE"}
            ).raise_for_status()

    def add_link(self, content: PassContent) -> str:
        """Regenerate the link without mutating anything (this serves a GET).

        The existence probe is a read, so the endpoint stays side-effect-free; it only
        decides whether the compact id-reference form is safe to use.
        """
        exists = False
        try:
            with self._api() as api:
                exists = api.get(
                    f"/genericObject/{self._object_id(content)}").status_code == 200
        except Exception:  # noqa: BLE001 - fall back to the self-contained link
            log.warning("could not probe the wallet object for card %s",
                        content.card_id, exc_info=True)
        return self._save_link(content, object_exists=exists)

    # --- internals ----------------------------------------------------------
    def _ensure_class(self, api: httpx.Client, content: PassContent) -> None:
        class_id = self._class_id(content)
        found = api.get(f"/genericClass/{class_id}")
        if found.status_code == 404:
            api.post("/genericClass", json=self._class_payload(content)).raise_for_status()
        else:
            found.raise_for_status()

    def _encode_save_jwt(self, sa: dict, payload: dict, origins: bool = True) -> str:
        claims = {
            "iss": sa["client_email"],
            "aud": "google",
            "typ": "savetowallet",
            "iat": int(time.time()),
            "payload": payload,
        }
        if origins:
            # Sites allowed to render the button for this token.
            claims["origins"] = settings.cors_origins
        return SAVE_LINK_BASE + jwt.encode(claims, sa["private_key"], algorithm="RS256")

    def _save_link(self, content: PassContent, *, object_exists: bool = False) -> str:
        """Signed "Add to Google Wallet" link.

        The JWT carries the FULL object definition, per Google's web guide, so the link
        can create the pass by itself — it does not depend on the REST pre-creation in
        `issue`, which is best-effort and must never block enrollment.

        The catch is size: Google states "the safe length of an encoded JWT is 1800
        characters… over 1800 characters, the save may not work due to truncation by web
        browsers", and our object exceeds that once a programme carries logo + hero image
        (their S3 URLs alone are ~130 characters each). So when the embedded form would
        be too long we fall back to Google's documented mitigation — reference the
        already-created object by id, which encodes in a few hundred characters. That
        fallback is only safe when the object really exists, hence `object_exists`;
        without that assurance an over-long embedded link still beats a short link
        pointing at nothing.
        """
        sa = self._credentials()
        obj = self._object_payload(content)
        # `origins` costs ~200 encoded characters and only matters to the JS button
        # API; the URL form does not need it, and the budget is better spent on content.
        link = self._encode_save_jwt(sa, {"genericObjects": [obj]}, origins=False)
        if len(link) <= SAVE_LINK_MAX_URL:
            return link

        if object_exists:
            short = self._encode_save_jwt(
                sa, {"genericObjects": [{"id": obj["id"]}]}, origins=False)
            log.info(
                "save link for card %s embedded %d chars (>%d); referencing the "
                "pre-created object instead (%d chars)",
                content.card_id, len(link), SAVE_LINK_MAX_URL, len(short))
            return short

        log.warning(
            "save link for card %s is %d chars (>%d) and the object is not known to "
            "exist, so it must stay self-contained — the URL may be truncated by the "
            "browser. Shorten the asset URLs to bring it under the limit.",
            content.card_id, len(link), SAVE_LINK_MAX_URL)
        return link
