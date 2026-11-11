"""Automation message texts (es/en), usted register — design: NOTIFICATIONS.md.

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
_DEFAULT_REWARD = {"es": "su recompensa", "en": "your reward"}


@dataclass(frozen=True)
class MessageFacts:
    """Everything a template may mention. Unused fields stay None."""

    mechanic: str = STAMPS            # "stamps" | "points"
    merchant: str = ""
    reward: str | None = None
    n: int | None = None              # gained (balance) or visit number (milestone)
    held: int | None = None
    target: int | None = None
    missing: int | None = None
    days: int | None = None


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


def render(kind: str, lang: str | None, facts: MessageFacts) -> str:
    """The body of automation `kind` (balance/ready/near/milestone/expiry/inactive)."""
    lang = normalize_language(lang)
    try:
        return _RENDERERS[kind](lang, facts)
    except KeyError:
        raise ValueError(f"no template for automation {kind!r}") from None


def _missing_es(f: MessageFacts) -> str:
    return "le falta" if f.missing == 1 else "le faltan"


def _balance(lang: str, f: MessageFacts) -> str:
    reward = reward_text(lang, f.reward)
    if lang == "es":
        gained = unit(lang, f.mechanic, f.n or 0)
        return (f"Sumó {gained}. Lleva {f.held} / {f.target}: {_missing_es(f)} "
                f"{f.missing} para su recompensa: {reward}.")
    return (f"You earned {unit(lang, f.mechanic, f.n or 0)}. You have {f.held} / {f.target}: "
            f"{f.missing} to go for {reward}.")


def _ready(lang: str, f: MessageFacts) -> str:
    reward = reward_text(lang, f.reward)
    if lang == "es":
        return (f"¡Completó su tarjeta! Su recompensa le espera: {reward}. "
                "Al canjearla, su tarjeta empieza de nuevo.")
    return (f"Card complete! Your reward is waiting: {reward}. "
            "Once redeemed, your card starts again.")


def _near(lang: str, f: MessageFacts) -> str:
    reward = reward_text(lang, f.reward)
    missing = unit(lang, f.mechanic, f.missing or 0)
    if lang == "es":
        verb = "Le falta" if f.missing == 1 else "Le faltan"
        return f"{verb} {missing} para su recompensa: {reward}. ¿Pasa hoy?"
    return f"Only {missing} to go for {reward}. Drop by today?"


def _milestone(lang: str, f: MessageFacts) -> str:
    if lang == "es":
        return f"¡Su visita n.º {f.n}! Gracias por volver a {f.merchant}."
    return f"Visit number {f.n}! Thank you for coming back to {f.merchant}."


def _expiry(lang: str, f: MessageFacts) -> str:
    held = unit(lang, f.mechanic, f.held or 0)
    days = days_text(lang, f.days or 0)
    one = f.held == 1
    if lang == "es":
        verb, use = ("vence", "Úselo") if one else ("vencen", "Úselos")
        possessive = "Su" if one else "Sus"
        return f"{possessive} {held} {verb} en {days}. {use} antes de perderlo{'' if one else 's'}."
    verb, pronoun = ("expires", "it") if one else ("expire", "them")
    return f"Your {held} {verb} in {days}. Use {pronoun} before you lose {pronoun}."


def _inactive(lang: str, f: MessageFacts) -> str:
    days = days_text(lang, f.days or 0)
    if lang == "es":
        return (f"Hace {days} que no le vemos. Lleva {f.held} / {f.target}: "
                "su recompensa está cerca.")
    return f"It's been {days}. You have {f.held} / {f.target}: your reward is close."


_RENDERERS = {"balance": _balance, "ready": _ready, "near": _near, "milestone": _milestone,
              "expiry": _expiry, "inactive": _inactive}
