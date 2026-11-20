"""Automation message texts (es/en), tú register, no exclamation marks — design: NOTIFICATIONS.md.

Kinds: balance, ready, near, inactive, level_up, birthday (fixed templates) and `sequence`
(always the merchant's own step text). A Pro merchant's custom text replaces the fixed one;
placeholders {negocio}, {premio}, {faltan} (a number), {nivel} are filled from the facts and
the result is cut to 150 characters (the Wallet message limit). Voice: branding.md.

Pure functions, no I/O. The header of every automation message is the business name; only
the body is rendered here. Singular/plural is handled per unit ("1 sello" / "2 sellos") so
a message never reads "1 sellos". Unknown languages fall back to Spanish (the cards
column only allows es/en, but a bad row must not break a send).
"""
from __future__ import annotations

from dataclasses import dataclass

STAMPS = "stamps"
POINTS = "points"

_UNITS = {
    "es": {STAMPS: ("sello", "sellos"), POINTS: ("punto", "puntos")},
    "en": {STAMPS: ("stamp", "stamps"), POINTS: ("point", "points")},
}
_DAYS = {"es": ("día", "días"), "en": ("day", "days")}
_DEFAULT_REWARD = {"es": "tu recompensa", "en": "your reward"}


@dataclass(frozen=True)
class MessageFacts:
    """Everything a template may mention. Unused fields stay None."""

    mechanic: str = STAMPS            # "stamps" | "points"
    merchant: str = ""
    reward: str | None = None
    n: int | None = None              # gained (balance)
    held: int | None = None
    target: int | None = None
    missing: int | None = None
    days: int | None = None
    level: int | None = None          # level_up: the new level


def normalize_language(lang: str | None) -> str:
    return lang if lang in _UNITS else "es"


def plural(lang: str, noun: tuple[str, str], n: int) -> str:
    """`n` + the noun in singular (n == 1) or plural form."""
    return f"{n} {noun[0] if n == 1 else noun[1]}"


def unit(lang: str, mechanic: str, n: int) -> str:
    return plural(lang, _UNITS[lang][mechanic], n)


def days_text(lang: str, n: int) -> str:
    return plural(lang, _DAYS[lang], n)


def reward_text(lang: str, reward: str | None) -> str:
    reward = (reward or "").strip()
    return reward or _DEFAULT_REWARD[lang]


MAX_BODY = 150
PLACEHOLDERS = ("{negocio}", "{premio}", "{faltan}", "{nivel}")


def fit(text: str, limit: int = MAX_BODY) -> str:
    """Cut to the Wallet body limit, on a word boundary when one is near."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit - 1].rstrip()
    space = cut.rfind(" ")
    if space >= limit - 25:
        cut = cut[:space].rstrip(" ,.;:")
    return cut + "…"


def fill(text: str, lang: str, facts: MessageFacts) -> str:
    """A merchant's custom text with its placeholders substituted."""
    values = {
        "{negocio}": facts.merchant or "",
        "{premio}": reward_text(lang, facts.reward),
        "{faltan}": "" if facts.missing is None else str(facts.missing),
        "{nivel}": "" if facts.level is None else str(facts.level),
    }
    for key, value in values.items():
        text = text.replace(key, value)
    return text


def render(kind: str, lang: str | None, facts: MessageFacts, custom: str | None = None) -> str:
    """The body of automation `kind`; `custom` (Pro) replaces the fixed template. At most
    150 characters."""
    lang = normalize_language(lang)
    if custom and custom.strip():
        return fit(fill(custom, lang, facts))
    if kind == "sequence":
        kind = "inactive"      # a step without text falls back to the basic reminder
    try:
        return fit(_RENDERERS[kind](lang, facts))
    except KeyError:
        raise ValueError(f"no template for automation {kind!r}") from None


def _missing_es(f: MessageFacts) -> str:
    return "te falta" if f.missing == 1 else "te faltan"


def _balance(lang: str, f: MessageFacts) -> str:
    reward = reward_text(lang, f.reward)
    if lang == "es":
        gained = unit(lang, f.mechanic, f.n or 0)
        return (f"Sumaste {gained}. Llevas {f.held} / {f.target}: {_missing_es(f)} "
                f"{f.missing} para tu recompensa: {reward}.")
    return (f"You earned {unit(lang, f.mechanic, f.n or 0)}. You have {f.held} / {f.target}: "
            f"{f.missing} to go for {reward}.")


def _ready(lang: str, f: MessageFacts) -> str:
    reward = reward_text(lang, f.reward)
    if lang == "es":
        return (f"Completaste tu tarjeta. Tu recompensa te espera: {reward}. "
                "Al canjearla, tu tarjeta empieza de nuevo.")
    return (f"Card complete. Your reward is waiting: {reward}. "
            "Once redeemed, your card starts again.")


def _near(lang: str, f: MessageFacts) -> str:
    reward = reward_text(lang, f.reward)
    missing = unit(lang, f.mechanic, f.missing or 0)
    if lang == "es":
        verb = "Te falta" if f.missing == 1 else "Te faltan"
        return f"{verb} {missing} para tu recompensa: {reward}. ¿Pasas hoy?"
    return f"Only {missing} to go for {reward}. Drop by today?"


def _inactive(lang: str, f: MessageFacts) -> str:
    days = days_text(lang, f.days or 0)
    if lang == "es":
        if f.held and f.target:
            return (f"Hace {days} que no te vemos. Llevas {f.held} / {f.target}: "
                    "tu recompensa está cerca.")
        return f"Hace {days} que no te vemos. Te esperamos en {f.merchant}."
    if f.held and f.target:
        return f"It's been {days}. You have {f.held} / {f.target}: your reward is close."
    return f"It's been {days} since we saw you. We're waiting for you at {f.merchant}."


def _level_up(lang: str, f: MessageFacts) -> str:
    if lang == "es":
        return f"Subiste a nivel {f.level}. Gracias por volver a {f.merchant}."
    return f"You reached level {f.level}. Thank you for coming back to {f.merchant}."


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text else text


def _birthday(lang: str, f: MessageFacts) -> str:
    prize = reward_text(lang, f.reward)
    days = days_text(lang, f.days or 0)
    if lang == "es":
        return (f"Feliz cumpleaños. Te regalamos {_lower_first(prize)}, válido {days} "
                f"en {f.merchant}.")
    return (f"Happy birthday. We're giving you {_lower_first(prize)}, valid for {days} "
            f"at {f.merchant}.")


_RENDERERS = {"balance": _balance, "ready": _ready, "near": _near, "inactive": _inactive,
              "level_up": _level_up, "birthday": _birthday}
