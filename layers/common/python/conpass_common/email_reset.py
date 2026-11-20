"""Password-reset code e-mail (D21): 6-digit code, ES "tú" register + EN. Pure function.

branding.md voice: tuteo, no exclamation marks, no emoji. The code is the only secret in the
body, so never log what this returns.
"""
from __future__ import annotations

from html import escape

from .email_templates import CONTACT

_COPY = {
    "es": {
        "subject": "Tu código para cambiar la contraseña de conpass",
        "intro": "Pediste cambiar la contraseña de tu cuenta de conpass. Usa este código:",
        "valid": "El código sirve durante {minutes} minutos y solo una vez.",
        "ignore": "Si no fuiste tú, ignora este correo: tu contraseña sigue igual.",
        "contact": f"¿Necesitas ayuda? Escríbenos a {CONTACT}.",
    },
    "en": {
        "subject": "Your code to change your conpass password",
        "intro": "You asked to change the password of your conpass account. Use this code:",
        "valid": "The code works for {minutes} minutes and only once.",
        "ignore": "If it wasn't you, ignore this email: your password stays the same.",
        "contact": f"Need help? Write to us at {CONTACT}.",
    },
}


def reset_code_email(lang: str | None, *, code: str, minutes: int = 15) -> tuple[str, str, str]:
    """Return (subject, text, html). Unknown `lang` falls back to Spanish."""
    c = _COPY.get(lang or "es", _COPY["es"])
    valid = c["valid"].format(minutes=minutes)
    text = "\n".join([c["intro"], "", code, "", valid, "", c["ignore"], c["contact"]])
    p = 'style="margin:0 0 12px"'
    html = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
        'line-height:1.5;color:#222;max-width:520px">'
        f"<p {p}>{escape(c['intro'])}</p>"
        '<p style="margin:0 0 16px"><span style="font-family:Menlo,Consolas,monospace;'
        'font-size:24px;letter-spacing:4px;background:#f3f3f3;border:1px solid #ddd;'
        f'border-radius:4px;padding:8px 14px;display:inline-block">{escape(code)}</span></p>'
        f"<p {p}>{escape(valid)}</p>"
        f'<p style="margin:0 0 12px;color:#666;font-size:13px">{escape(c["ignore"])}<br>'
        f'{escape(c["contact"])}</p>'
        "</div>"
    )
    return c["subject"], text, html
