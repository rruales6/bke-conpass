"""Credential email bodies (ES "usted" register + EN). Pure functions, no I/O.

Every interpolated value goes through html.escape; styles are inline; kept short and
plain (text + simple HTML) to stay out of spam folders. Never log what these return:
the bodies contain a temporary password.
"""
from __future__ import annotations

from html import escape

CONTACT = "info@conpass.cards"

_COPY = {
    "es": {
        "subject": "Su cuenta de conpass está lista",
        "subject_resend": "Sus nuevas credenciales de conpass",
        "greeting": "Hola {owner},",
        "greeting_anon": "Hola,",
        "intro": "La cuenta de {business} en conpass está lista.",
        "intro_resend": "Hemos generado nuevas credenciales de acceso para {business} en conpass.",
        "email_label": "Correo de acceso",
        "password_label": "Contraseña temporal",
        "login": "Ingrese aquí",
        "change": "Deberá cambiarla al ingresar por primera vez.",
        "ignore": "Si no solicitó esta cuenta, ignore este correo.",
        "contact": f"¿Necesita ayuda? Escríbanos a {CONTACT}.",
    },
    "en": {
        "subject": "Your conpass account is ready",
        "subject_resend": "Your new conpass credentials",
        "greeting": "Hello {owner},",
        "greeting_anon": "Hello,",
        "intro": "The conpass account for {business} is ready.",
        "intro_resend": "We have generated new login credentials for {business} on conpass.",
        "email_label": "Login email",
        "password_label": "Temporary password",
        "login": "Log in here",
        "change": "You'll be asked to change it at first login.",
        "ignore": "If you didn't request this account, please ignore this email.",
        "contact": f"Need help? Write to us at {CONTACT}.",
    },
}


def credentials_email(
    lang: str | None,
    *,
    owner_name: str | None,
    business_name: str,
    email: str,
    temp_password: str,
    login_url: str,
    resend: bool = False,
) -> tuple[str, str, str]:
    """Return (subject, text, html). Unknown `lang` falls back to Spanish."""
    c = _COPY.get(lang or "es", _COPY["es"])
    owner = owner_name or ""
    subject = c["subject_resend"] if resend else c["subject"]
    intro = c["intro_resend"] if resend else c["intro"]
    greeting = c["greeting"] if owner else c["greeting_anon"]

    def _fill(tpl: str, esc) -> str:
        return tpl.format(owner=esc(owner), business=esc(business_name))

    text = "\n".join([
        _fill(greeting, str),
        "",
        _fill(intro, str),
        "",
        f"{c['email_label']}: {email}",
        f"{c['password_label']}: {temp_password}",
        f"{c['login']}: {login_url}",
        "",
        c["change"],
        c["ignore"],
        c["contact"],
    ])

    p = 'style="margin:0 0 12px"'
    html = (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
        'line-height:1.5;color:#222;max-width:520px">'
        f"<p {p}>{_fill(greeting, escape)}</p>"
        f"<p {p}>{_fill(intro, escape)}</p>"
        f"<p {p}>{escape(c['email_label'])}: <strong>{escape(email)}</strong></p>"
        f"<p {p}>{escape(c['password_label'])}:</p>"
        '<p style="margin:0 0 16px"><span style="font-family:Menlo,Consolas,monospace;'
        'font-size:17px;background:#f3f3f3;border:1px solid #ddd;border-radius:4px;'
        f'padding:8px 12px;display:inline-block">{escape(temp_password)}</span></p>'
        f'<p {p}><a href="{escape(login_url, quote=True)}">{escape(c["login"])}</a></p>'
        f"<p {p}>{escape(c['change'])}</p>"
        f'<p style="margin:0 0 12px;color:#666;font-size:13px">{escape(c["ignore"])}<br>'
        f'{escape(c["contact"])}</p>'
        "</div>"
    )
    return subject, text, html
