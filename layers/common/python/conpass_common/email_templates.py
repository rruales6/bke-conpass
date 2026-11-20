"""Login-details email bodies (ES "tú" register + EN). Pure functions, no I/O.

Every interpolated value goes through html.escape; styles are inline; kept short and
plain (text + simple HTML) to stay out of spam folders. Never log what these return:
the bodies contain a temporary password.
"""
from __future__ import annotations

from html import escape

CONTACT = "info@conpass.cards"

_COPY = {
    "es": {
        "subject": "Tu cuenta de conpass está lista",
        "subject_resend": "Tus nuevos datos de acceso a conpass",
        "greeting": "Hola {owner},",
        "greeting_anon": "Hola,",
        "intro": "La cuenta de {business} en conpass está lista.",
        "intro_resend": "Generamos nuevos datos de acceso para {business} en conpass.",
        "email_label": "Correo de acceso",
        "password_label": "Contraseña temporal",
        "login": "Entra aquí",
        "change": "Cámbiala la primera vez que entres.",
        "ignore": "Si no pediste esta cuenta, ignora este correo.",
        "contact": f"¿Necesitas ayuda? Escríbenos a {CONTACT}.",
    },
    "en": {
        "subject": "Your conpass account is ready",
        "subject_resend": "Your new conpass login details",
        "greeting": "Hello {owner},",
        "greeting_anon": "Hello,",
        "intro": "The conpass account for {business} is ready.",
        "intro_resend": "We generated new login details for {business} on conpass.",
        "email_label": "Login email",
        "password_label": "Temporary password",
        "login": "Log in here",
        "change": "Change it the first time you log in.",
        "ignore": "If you didn't request this account, ignore this email.",
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


# --------------------------------------------------------------------------- #
# Phase 18 (D21): activation, rejection, reminders and plan changes.
#
# Voice follows branding.md: "tú", no exclamation marks, no emoji, "datos de acceso"
# (never "credenciales"), concrete amounts with "IVA incluido". Same rules as above:
# pure functions, every interpolated value escaped in the HTML body.
# --------------------------------------------------------------------------- #
_TIER = {"starter": "Starter", "growth": "Growth", "pro": "Pro"}

_COPY18 = {
    "es": {
        "greeting": "Hola {owner},",
        "greeting_anon": "Hola,",
        "contact": f"¿Necesitas ayuda? Escríbenos a {CONTACT}.",
        "vat": "IVA incluido",
        "period": {"monthly": "mensual", "annual": "anual"},
        "per": {"monthly": "al mes", "annual": "al año"},
        "pay_title": "Datos para el pago",
        "pay_bank": "Banco",
        "pay_type": "Tipo de cuenta",
        "pay_number": "Número de cuenta",
        "pay_holder": "Titular",
        "pay_tax_id": "RUC o cédula del titular",
        "pay_none": f"Escríbenos a {CONTACT} y te enviamos los datos de pago.",
        "activation_subject": "Activamos tu cuenta de conpass",
        "activation_intro": "Activamos tu cuenta: {business} ya puede usar conpass.",
        "activation_access": "Estos son tus datos de acceso:",
        "email_label": "Correo de acceso",
        "password_label": "Contraseña temporal",
        "login": "Entra aquí",
        "change": "Cámbiala la primera vez que entres.",
        "rejected_subject": "No pudimos activar tu cuenta de conpass",
        "rejected_intro": (
            "Revisamos la solicitud de {business} y "
            "todavía no podemos activar la cuenta."
        ),
        "rejected_reason": "Motivo: {reason}",
        "rejected_next": (
            "Corrige lo indicado y responde a "
            "este correo, o escríbenos para revisarlo juntos."
        ),
        "reminder_subject": "Tu pago de conpass está pendiente",
        "reminder_intro": "Te recordamos que el pago de conpass de {business} está pendiente.",
        "reminder_amount": "Total a pagar: {amount} ({vat}).",
        "reminder_next": (
            "Cuando hagas la transferencia, avísanos respondiendo "
            "a este correo con el comprobante."
        ),
        "change_pay_subject": "Datos para pagar tu cambio de plan",
        "change_pay_intro": "Recibimos tu solicitud para pasar {business} al plan {tier} {period}.",
        "change_pay_amount": "Total a transferir: {amount} ({vat}).",
        "change_pay_next": (
            "Sube el comprobante desde Ajustes, Plan. Cuando "
            "lo confirmemos, el plan cambia ese mismo día."
        ),
        "applied_subject": "Tu plan de conpass cambió",
        "applied_intro": "{business} ya está en el plan {tier} {period}.",
        "applied_next": "Tu próximo cobro es el {date}.",
        "change_rejected_subject": "No pudimos aplicar tu cambio de plan",
        "change_rejected_intro": (
            "Revisamos tu solicitud de cambio de "
            "plan de {business} y no pudimos aplicarla."
        ),
        "change_rejected_next": (
            "Tu plan actual sigue igual. "
            "Si quieres intentarlo de nuevo, escríbenos."
        ),
    },
    "en": {
        "greeting": "Hello {owner},",
        "greeting_anon": "Hello,",
        "contact": f"Need help? Write to us at {CONTACT}.",
        "vat": "VAT included",
        "period": {"monthly": "monthly", "annual": "annual"},
        "per": {"monthly": "per month", "annual": "per year"},
        "pay_title": "Payment details",
        "pay_bank": "Bank",
        "pay_type": "Account type",
        "pay_number": "Account number",
        "pay_holder": "Account holder",
        "pay_tax_id": "Holder tax ID",
        "pay_none": f"Write to us at {CONTACT} and we will send you the payment details.",
        "activation_subject": "We activated your conpass account",
        "activation_intro": "We activated your account: {business} can now use conpass.",
        "activation_access": "Here are your login details:",
        "email_label": "Login email",
        "password_label": "Temporary password",
        "login": "Log in here",
        "change": "Change it the first time you log in.",
        "rejected_subject": "We could not activate your conpass account",
        "rejected_intro": (
            "We reviewed the request for {business} "
            "and cannot activate the account yet."
        ),
        "rejected_reason": "Reason: {reason}",
        "rejected_next": (
            "Fix what is noted and reply to this email, "
            "or write to us and we will review it together."
        ),
        "reminder_subject": "Your conpass payment is pending",
        "reminder_intro": "A reminder that the conpass payment for {business} is pending.",
        "reminder_amount": "Amount due: {amount} ({vat}).",
        "reminder_next": "Once you have made the transfer, reply to this email with the receipt.",
        "change_pay_subject": "Payment details for your plan change",
        "change_pay_intro": (
            "We received your request to move "
            "{business} to the {tier} {period} plan."
        ),
        "change_pay_amount": "Amount to transfer: {amount} ({vat}).",
        "change_pay_next": (
            "Upload the receipt from Settings, Plan. Once we "
            "confirm it, the plan changes the same day."
        ),
        "applied_subject": "Your conpass plan changed",
        "applied_intro": "{business} is now on the {tier} {period} plan.",
        "applied_next": "Your next charge is on {date}.",
        "change_rejected_subject": "We could not apply your plan change",
        "change_rejected_intro": (
            "We reviewed the plan change request "
            "for {business} and could not apply it."
        ),
        "change_rejected_next": (
            "Your current plan stays the same. If "
            "you want to try again, write to us."
        ),
    },
}


def _c18(lang: str | None) -> dict:
    return _COPY18.get(lang or "es", _COPY18["es"])


def _usd(amount) -> str:
    return f"${float(amount):.2f}"


def _compose(blocks: list[tuple]) -> tuple[str, str]:
    """Blocks → (text, html). Kinds: ("p", text), ("kv", label, value), ("code", value),
    ("link", label, url), ("title", text), ("muted", text). Values are escaped for HTML."""
    text_lines: list[str] = []
    html_parts: list[str] = []
    p = 'style="margin:0 0 12px"'
    for block in blocks:
        kind = block[0]
        if kind == "p":
            text_lines += [block[1], ""]
            html_parts.append(f"<p {p}>{escape(block[1])}</p>")
        elif kind == "title":
            text_lines += [block[1], ""]
            html_parts.append(f'<p style="margin:0 0 6px"><strong>{escape(block[1])}</strong></p>')
        elif kind == "kv":
            text_lines.append(f"{block[1]}: {block[2]}")
            html_parts.append(f'<p style="margin:0 0 4px">{escape(block[1])}: '
                              f"<strong>{escape(block[2])}</strong></p>")
        elif kind == "code":
            text_lines.append(block[1])
            html_parts.append(
                '<p style="margin:0 0 16px"><span style="font-family:Menlo,Consolas,monospace;'
                'font-size:17px;background:#f3f3f3;border:1px solid #ddd;border-radius:4px;'
                f'padding:8px 12px;display:inline-block">{escape(block[1])}</span></p>')
        elif kind == "link":
            text_lines.append(f"{block[1]}: {block[2]}")
            html_parts.append(f'<p {p}><a href="{escape(block[2], quote=True)}">'
                              f"{escape(block[1])}</a></p>")
        elif kind == "muted":
            text_lines += ["", block[1]]
            html_parts.append(f'<p style="margin:0 0 12px;color:#666;font-size:13px">'
                              f"{escape(block[1])}</p>")
        elif kind == "gap":
            text_lines.append("")
    html = ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
            'line-height:1.5;color:#222;max-width:520px">' + "".join(html_parts) + "</div>")
    return "\n".join(text_lines).rstrip() + "\n", html


def _greeting(c: dict, owner_name: str | None) -> tuple:
    return ("p", c["greeting"].format(owner=owner_name) if owner_name else c["greeting_anon"])


def _payment_blocks(c: dict, settings_row: dict | None) -> list[tuple]:
    """The platform's transfer details (platform_payment_settings row), or a contact line when
    the account is not configured yet."""
    row = settings_row or {}
    if not row.get("account_number"):
        return [("title", c["pay_title"]), ("p", c["pay_none"])]
    pairs = [(c["pay_bank"], row.get("bank_name")), (c["pay_type"], row.get("account_type")),
             (c["pay_number"], row.get("account_number")),
             (c["pay_holder"], row.get("beneficiary_name")),
             (c["pay_tax_id"], row.get("beneficiary_tax_id"))]
    blocks: list[tuple] = [("title", c["pay_title"])]
    blocks += [("kv", label, str(value)) for label, value in pairs if value]
    if row.get("instructions"):
        blocks += [("gap",), ("p", str(row["instructions"]))]
    return blocks


def activation_email(lang: str | None, *, owner_name: str | None, business_name: str,
                     email: str, temp_password: str, login_url: str) -> tuple[str, str, str]:
    """Sent when platform-admin activates a signup: the account is live and these are the
    access details (D21: no longer e-mailed at signup). Never log what this returns."""
    c = _c18(lang)
    text, html = _compose([
        _greeting(c, owner_name),
        ("p", c["activation_intro"].format(business=business_name)),
        ("p", c["activation_access"]),
        ("kv", c["email_label"], email),
        ("p", c["password_label"] + ":"),
        ("code", temp_password),
        ("link", c["login"], login_url),
        ("p", c["change"]),
        ("muted", c["contact"]),
    ])
    return c["activation_subject"], text, html


def signup_rejected_email(lang: str | None, *, owner_name: str | None, business_name: str,
                          reason: str) -> tuple[str, str, str]:
    c = _c18(lang)
    text, html = _compose([
        _greeting(c, owner_name),
        ("p", c["rejected_intro"].format(business=business_name)),
        ("p", c["rejected_reason"].format(reason=reason)),
        ("p", c["rejected_next"]),
        ("muted", c["contact"]),
    ])
    return c["rejected_subject"], text, html


def payment_reminder_email(lang: str | None, *, owner_name: str | None, business_name: str,
                           amount_usd, payment_settings: dict | None) -> tuple[str, str, str]:
    c = _c18(lang)
    text, html = _compose([
        _greeting(c, owner_name),
        ("p", c["reminder_intro"].format(business=business_name)),
        ("p", c["reminder_amount"].format(amount=_usd(amount_usd), vat=c["vat"])),
        *_payment_blocks(c, payment_settings),
        ("gap",),
        ("p", c["reminder_next"]),
        ("muted", c["contact"]),
    ])
    return c["reminder_subject"], text, html


def plan_change_payment_email(lang: str | None, *, owner_name: str | None, business_name: str,
                              tier: str, period: str, amount_usd,
                              payment_settings: dict | None) -> tuple[str, str, str]:
    c = _c18(lang)
    text, html = _compose([
        _greeting(c, owner_name),
        ("p", c["change_pay_intro"].format(
            business=business_name, tier=_TIER.get(tier, tier), period=c["period"][period])),
        ("p", c["change_pay_amount"].format(amount=_usd(amount_usd), vat=c["vat"])),
        *_payment_blocks(c, payment_settings),
        ("gap",),
        ("p", c["change_pay_next"]),
        ("muted", c["contact"]),
    ])
    return c["change_pay_subject"], text, html


def plan_change_applied_email(lang: str | None, *, owner_name: str | None, business_name: str,
                              tier: str, period: str,
                              next_charge_at: str | None) -> tuple[str, str, str]:
    c = _c18(lang)
    blocks: list[tuple] = [
        _greeting(c, owner_name),
        ("p", c["applied_intro"].format(
            business=business_name, tier=_TIER.get(tier, tier), period=c["period"][period])),
    ]
    if next_charge_at:
        blocks.append(("p", c["applied_next"].format(date=next_charge_at)))
    blocks.append(("muted", c["contact"]))
    text, html = _compose(blocks)
    return c["applied_subject"], text, html


def plan_change_rejected_email(lang: str | None, *, owner_name: str | None, business_name: str,
                               reason: str) -> tuple[str, str, str]:
    c = _c18(lang)
    text, html = _compose([
        _greeting(c, owner_name),
        ("p", c["change_rejected_intro"].format(business=business_name)),
        ("p", c["rejected_reason"].format(reason=reason)),
        ("p", c["change_rejected_next"]),
        ("muted", c["contact"]),
    ])
    return c["change_rejected_subject"], text, html
