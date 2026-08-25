"""Google Wallet provider — single-issuer model (B2, D8).

Implements the `WalletProvider` interface against the Google Wallet REST API using
**Generic** passes (one class per program, one object per card). The backend is the
authority for balances; this provider only reflects that state into the wallet:
`issue` creates/refreshes the object and returns a signed "Add to Google Wallet" save
link, `update` pushes new balances to an already-installed pass, `revoke` deactivates it.

Layout (D14): the class carries a `cardTemplateOverride` describing the rows drawn on the
FRONT of the card, each cell bound to one of the object's `textModulesData` entries. That
split is what makes a rich pass possible at all — untemplated text modules only appear
once the holder expands the pass, which is too late for "how many stamps do I have".

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
from .wallet import IssuedPass, PassContent, WalletKind, WalletProvider, normalize_language

GOOGLE_WALLET_SCOPE = "https://www.googleapis.com/auth/wallet_object.issuer"
SAVE_LINK_BASE = "https://pay.google.com/gp/v/save/"
API_BASE = "https://walletobjects.googleapis.com/walletobjects/v1"
_TOKEN_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_HTTP_TIMEOUT = 10.0
# A save link is handed to a browser as a URL, so the signed JWT has to stay addressable.
# Google's guidance for the URL form is ~1800 characters; past that the button should use
# the JS API instead. We log rather than truncate — a shortened pass would be worse.
SAVE_LINK_MAX_URL = 1800
# Past a dozen, a stamp strip stops being readable at a glance and the numeric progress
# cell carries the meaning on its own.
MAX_STAMP_DOTS = 12
_FILLED, _EMPTY = "●", "○"  # ● ○
_PLACEHOLDER = "—"  # em dash — a templated cell with nothing to say

log = logging.getLogger(__name__)

# Every string the holder reads. The pass renders ONE language, chosen when the customer
# enrolled (D16) — Google's `translatedValues` would need the holder's device locale to
# match, which is not what a merchant in Ecuador means by "this card is in Spanish".
_LABELS: dict[str, dict[str, str]] = {
    "es": {
        "loyalty_stamps": "Tarjeta de lealtad",
        "loyalty_points": "Tarjeta de lealtad",
        "membership_pass": "Pase de membresía",
        "stamps": "SELLOS",
        "progress": "PROGRESO",
        "points": "PUNTOS",
        "goal": "META",
        "rewards": "PREMIOS",
        "status": "ESTADO",
        "valid_until": "VÁLIDA HASTA",
        "includes": "INCLUYE",
        "reward": "RECOMPENSA",
        "member": "MIEMBRO",
        "email": "CORREO",
        "active": "Activa",
        "no_expiry": "Sin caducidad",
    },
    "en": {
        "loyalty_stamps": "Stamp card",
        "loyalty_points": "Points card",
        "membership_pass": "Membership pass",
        "stamps": "STAMPS",
        "progress": "PROGRESS",
        "points": "POINTS",
        "goal": "GOAL",
        "rewards": "REWARDS",
        "status": "STATUS",
        "valid_until": "VALID UNTIL",
        "includes": "INCLUDES",
        "reward": "PROGRAM REWARD",
        "member": "MEMBER",
        "email": "EMAIL",
        "active": "Active",
        "no_expiry": "No expiry",
    },
}


def _loc(value: str, lang: str) -> dict:
    """A Google `LocalizedString` in the card's own language."""
    return {"defaultValue": {"language": lang, "value": value}}


def _cell(module_id: str) -> dict:
    """One template cell, bound to an object text module (label + value)."""
    return {"firstValue": {"fields": [{"fieldPath": f"object.textModulesData['{module_id}']"}]}}


def _row_one(a: str) -> dict:
    return {"oneItem": {"item": _cell(a)}}


def _row_two(a: str, b: str) -> dict:
    return {"twoItems": {"startItem": _cell(a), "endItem": _cell(b)}}


def _row_three(a: str, b: str, c: str) -> dict:
    return {"threeItems": {"startItem": _cell(a), "middleItem": _cell(b), "endItem": _cell(c)}}


def stamp_strip(stamps: int, target: int | None) -> str | None:
    """`●●○○○○○○` for the progress toward the next reward, or None when it would not read.

    Needs a target to have any meaning, and stays out of the way once the target is large
    enough that the dots blur together.
    """
    if not target or target > MAX_STAMP_DOTS:
        return None
    filled = max(0, min(stamps, target))
    return _FILLED * filled + _EMPTY * (target - filled)


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

    def _card_rows(self, content: PassContent) -> list[dict]:
        """The rows drawn on the front of the card.

        Row shape is a PROGRAM decision, so it belongs on the class: which cells exist
        depends on the program's type and whether it defines a reward. Per-CARD gaps
        (a customer who enrolled without a name or email) cannot vary the template, so
        those cells always exist and the object fills them with a placeholder.
        """
        rows = [_row_three("hdr0", "hdr1", "hdr2")]
        if content.reward_text:
            # Full width: a reward is a sentence ("El 8.º café es gratis"), not a figure.
            rows.append(_row_one("sec0"))
        rows.append(_row_two("sec1", "sec2"))
        return rows

    def _class_payload(self, content: PassContent) -> dict:
        return {
            "id": self._class_id(content),
            "issuerName": content.merchant_name,
            "reviewStatus": "UNDER_REVIEW",
            "multipleDevicesAndHoldersAllowedStatus": "ONE_USER_ALL_DEVICES",
            "classTemplateInfo": {
                "cardTemplateOverride": {"cardRowTemplateInfos": self._card_rows(content)}
            },
        }

    def _text_modules(self, content: PassContent) -> list[dict]:
        """The values behind the card-front cells, in the card's language.

        Ids are positional and must match `_card_rows`: `hdr*` is the status row, `sec*`
        the rows under it. Every id the template can reference is emitted — an unbound
        cell renders blank, which looks like a broken pass rather than an absent value.
        """
        lang = normalize_language(content.language)
        label = _LABELS[lang]
        mods: list[dict] = []

        def add(module_id: str, key: str, body: str | None) -> None:
            mods.append({"id": module_id, "header": label[key], "body": body or _PLACEHOLDER})

        if content.program_type == "loyalty_points":
            total = content.points_for_reward
            add("hdr0", "points", str(content.points))
            add("hdr1", "goal", str(total) if total else None)
            add("hdr2", "rewards", str(content.rewards_available))
        elif content.program_type == "membership_pass":
            add("hdr0", "status", label["active"])
            add("hdr1", "valid_until", content.membership_active_until or label["no_expiry"])
            add("hdr2", "includes", content.membership_includes)
        else:  # loyalty_stamps — the default mechanic
            total = content.stamps_for_reward
            strip = stamp_strip(content.stamps, total)
            add("hdr0", "stamps", strip or str(content.stamps))
            # The dots are for recognition, the figure for reading. When the strip is too
            # long to draw, the first cell already holds the count and this one the target.
            if strip:
                add("hdr1", "progress", f"{min(content.stamps, total or 0)} / {total}")
            else:
                add("hdr1", "goal", str(total) if total else None)
            add("hdr2", "rewards", str(content.rewards_available))

        if content.reward_text:
            add("sec0", "reward", content.reward_text)
        add("sec1", "member", content.holder_name)
        add("sec2", "email", content.member_email)
        return mods

    def _object_payload(self, content: PassContent) -> dict:
        lang = normalize_language(content.language)
        obj: dict = {
            "id": self._object_id(content),
            "classId": self._class_id(content),
            "state": "ACTIVE",
            "cardTitle": _loc(content.merchant_name, lang),
            # subheader is the small kicker above the title, so it names the KIND of card
            # and the title is the programme itself. The balance is no longer squeezed in
            # here — it has its own row on the card front (D14).
            "subheader": _loc(_LABELS[lang][content.program_type], lang),
            "header": _loc(content.program_name, lang),
            "barcode": {"type": "QR_CODE", "value": content.opaque_token},
            "textModulesData": self._text_modules(content),
        }
        if content.accent_color and _HEX_RE.match(content.accent_color):
            obj["hexBackgroundColor"] = content.accent_color
        # No contentDescription on either image: cardTitle/header already say the same
        # thing in text, and it is dead weight in every payload.
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

    def sync_program(self, content: PassContent) -> None:
        """Refresh the program's class — the card-front row template lives there."""
        with self._api() as api:
            self._ensure_class(api, content)

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
        """Create the class, or bring an existing one up to date.

        Creating-only would strand every program that already has a class: the card-front
        template is a class field, so a class written before this layout existed would
        keep rendering the old pass forever. A PUT is idempotent, so re-running costs a
        request and changes nothing when the payload already matches.
        """
        class_id = self._class_id(content)
        payload = self._class_payload(content)
        found = api.get(f"/genericClass/{class_id}")
        if found.status_code == 404:
            api.post("/genericClass", json=payload).raise_for_status()
        else:
            found.raise_for_status()
            api.put(f"/genericClass/{class_id}", json=payload).raise_for_status()

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

        When the object already exists the link just **references it by id** (~705
        characters). That is Google's own mitigation for the URL-length limit, and making
        it the primary path — rather than a rescue for oversized payloads — is what lets
        the pass carry as much content as the layout needs. Google states "the safe length
        of an encoded JWT is 1800 characters… over 1800 characters, the save may not work
        due to truncation by web browsers", and a pass with a logo, a hero image and the
        card-front rows is comfortably past that (D13).

        Without that assurance the link has to carry the whole object, so that it can
        create the pass by itself: `issue()`'s REST pre-creation is best-effort and must
        never block enrollment, so the link cannot depend on it having worked.
        """
        sa = self._credentials()
        obj = self._object_payload(content)
        if object_exists:
            return self._encode_save_jwt(sa, {"genericObjects": [{"id": obj["id"]}]},
                                         origins=False)

        # `origins` costs ~200 encoded characters and only matters to the JS button
        # API; the URL form does not need it, and the budget is better spent on content.
        link = self._encode_save_jwt(sa, {"genericObjects": [obj]}, origins=False)
        if len(link) > SAVE_LINK_MAX_URL:
            log.warning(
                "save link for card %s is %d chars (>%d) and the object is not known to "
                "exist, so it must stay self-contained — the URL may be truncated by the "
                "browser. Shorten the asset URLs to bring it under the limit.",
                content.card_id, len(link), SAVE_LINK_MAX_URL)
        return link
