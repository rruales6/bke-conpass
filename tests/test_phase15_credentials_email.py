"""Phase 15 tests — emailed owner credentials + forced first-login password change.

Fully mocked: a fake smtplib, a fake Supabase admin client and fake repos. No network,
no SMTP connection, no real email.
"""
from __future__ import annotations

import smtplib
import types

import pytest
from conpass_common import auth as auth_mod
from conpass_common import provisioning as prov
from conpass_common.app import require_identity
from conpass_common.auth import Identity
from conpass_common.demo import DEMO_OWNER_EMAIL
from conpass_common.email_templates import credentials_email
from conpass_common.errors import ValidationFailed
from conpass_common.providers import factory
from conpass_common.providers.email import (
    EmailDeliveryError,
    SmtpEmailSender,
    StubEmailSender,
)
from fastapi.testclient import TestClient
from supabase_auth.errors import AuthApiError, AuthWeakPasswordError

U1 = "11111111-1111-1111-1111-111111111111"
M1 = "22222222-2222-2222-2222-222222222222"
PW = "Tmp-Pw-Secret-9"


def _identity(app, monkeypatch, roles=("platform_admin",), merchant_id=None,
              email="a@b.co"):
    monkeypatch.setitem(app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email=email, roles=list(roles), merchant_id=merchant_id))


# --------------------------------------------------------------------------- #
# SmtpEmailSender (fake smtplib)
# --------------------------------------------------------------------------- #
class _FakeSMTP:
    instances: list[_FakeSMTP] = []
    login_error: Exception | None = None
    connect_error: Exception | None = None

    def __init__(self, host, port, **kw):
        if type(self).connect_error:
            raise type(self).connect_error
        self.host, self.port, self.kw = host, port, kw
        self.calls: list[str] = []
        self.sent = []
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        if type(self).login_error:
            raise type(self).login_error
        self.calls.append("login")
        self.creds = (user, password)

    def send_message(self, msg):
        self.calls.append("send")
        self.sent.append(msg)


@pytest.fixture
def fake_smtp(monkeypatch):
    class S(_FakeSMTP):
        instances: list = []
        login_error = None
        connect_error = None
    monkeypatch.setattr(smtplib, "SMTP_SSL", S)
    monkeypatch.setattr(smtplib, "SMTP", S)
    return S


def _sender(**over):
    kw = dict(host="smtp.example.test", port=465, username="no-reply@conpass.cards",
              password="pw", from_address="no-reply@conpass.cards", from_name="conpass",
              reply_to="info@conpass.cards")
    kw.update(over)
    return SmtpEmailSender(**kw)


def test_smtp_sends_multipart_with_headers(fake_smtp):
    _sender().send("ana@test.co", "Hola", "texto plano", "<p>html</p>")
    smtp = fake_smtp.instances[0]
    assert smtp.calls == ["login", "send"]          # implicit TLS: no starttls
    assert smtp.creds == ("no-reply@conpass.cards", "pw")
    msg = smtp.sent[0]
    assert msg["To"] == "ana@test.co"
    assert msg["From"] == "conpass <no-reply@conpass.cards>"
    assert msg["Reply-To"] == "info@conpass.cards"
    assert msg["Auto-Submitted"] == "auto-generated"
    assert msg["Message-ID"].rstrip(">").endswith("@conpass.cards")
    assert msg.is_multipart()
    assert {p.get_content_type() for p in msg.iter_parts()} == {"text/plain", "text/html"}


def test_smtp_port_587_uses_starttls(fake_smtp):
    _sender(port=587).send("a@b.co", "s", "t")
    assert fake_smtp.instances[0].calls == ["starttls", "login", "send"]


def test_smtp_auth_error_becomes_delivery_error(fake_smtp):
    fake_smtp.login_error = smtplib.SMTPAuthenticationError(535, b"nope")
    with pytest.raises(EmailDeliveryError):
        _sender().send("a@b.co", "s", "t")


def test_smtp_timeout_becomes_delivery_error(fake_smtp):
    fake_smtp.connect_error = TimeoutError("timed out")
    with pytest.raises(EmailDeliveryError):
        _sender().send("a@b.co", "s", "t")


def test_smtp_unconfigured(fake_smtp):
    s = _sender(password=None)
    assert s.configured is False
    with pytest.raises(EmailDeliveryError):
        s.send("a@b.co", "s", "t")
    assert fake_smtp.instances == []                # never even connected


def test_stub_sender_is_configured_and_silent():
    s = StubEmailSender()
    assert s.configured is True
    s.send("a@b.co", "s", "body-secret")            # does not raise


# --------------------------------------------------------------------------- #
# Factory + config
# --------------------------------------------------------------------------- #
def test_factory_defaults_to_stub(monkeypatch):
    monkeypatch.delenv("CONPASS_EMAIL_PROVIDER", raising=False)
    factory.get_email_sender.cache_clear()
    assert isinstance(factory.get_email_sender(), StubEmailSender)
    factory.get_email_sender.cache_clear()


def test_factory_selects_smtp_without_credentials_never_stub(monkeypatch):
    monkeypatch.setenv("CONPASS_EMAIL_PROVIDER", "smtp")
    monkeypatch.setenv("SMTP_USERNAME", "")
    monkeypatch.setenv("SMTP_PASSWORD", "")
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("SMTP_PORT", "")
    monkeypatch.setenv("CONPASS_SECRETS_FILE", "/nonexistent/secrets.yaml")
    from conpass_common import config
    config._secrets.cache_clear()
    factory.get_email_sender.cache_clear()
    try:
        sender = factory.get_email_sender()
        assert isinstance(sender, SmtpEmailSender)
        assert sender.configured is False
        assert sender.host == "smtp.hostinger.com" and sender.port == 465  # "" = unset
    finally:
        factory.get_email_sender.cache_clear()
        config._secrets.cache_clear()


def test_factory_smtp_with_env_credentials(monkeypatch):
    monkeypatch.setenv("CONPASS_EMAIL_PROVIDER", "smtp")
    monkeypatch.setenv("SMTP_USERNAME", "no-reply@conpass.cards")
    monkeypatch.setenv("SMTP_PASSWORD", "x")
    monkeypatch.setenv("SMTP_PORT", "587")
    factory.get_email_sender.cache_clear()
    try:
        s = factory.get_email_sender()
        assert s.configured and s.port == 587
        assert s.from_address == "no-reply@conpass.cards"
    finally:
        factory.get_email_sender.cache_clear()


def test_factory_unknown_provider(monkeypatch):
    monkeypatch.setenv("CONPASS_EMAIL_PROVIDER", "carrier-pigeon")
    factory.get_email_sender.cache_clear()
    try:
        with pytest.raises(ValueError):
            factory.get_email_sender()
    finally:
        factory.get_email_sender.cache_clear()


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #
def _tpl(lang, **over):
    kw = dict(owner_name="Ana", business_name="Café Sol", email="ana@test.co",
              temp_password=PW, login_url="https://console.conpass.cards/login")
    kw.update(over)
    return credentials_email(lang, **kw)


@pytest.mark.parametrize("lang,marker", [("es", "Deberá cambiarla"),
                                         ("en", "asked to change it")])
def test_template_content(lang, marker):
    subject, text, html = _tpl(lang)
    for body in (text, html):
        assert PW in body and "ana@test.co" in body and "Café Sol" in body
        assert "https://console.conpass.cards/login" in body
        assert marker in body
        assert "info@conpass.cards" in body
    assert "monospace" in html.lower()
    assert subject


def test_template_resend_and_fallback_language():
    s_new, _, _ = _tpl("es")
    s_resend, _, _ = _tpl("es", resend=True)
    assert s_new != s_resend and "nuevas" in s_resend
    assert _tpl("fr")[0] == s_new                   # unknown -> Spanish


def test_template_escapes_html():
    _, text, html = _tpl("es", business_name="<script>alert(1)</script>",
                         owner_name='"><img src=x>', temp_password="a<b>&c")
    assert "<script>" not in html and "<img" not in html
    assert "&lt;script&gt;" in html and "a&lt;b&gt;&amp;c" in html
    assert "<script>" in text                       # plain text stays verbatim


# --------------------------------------------------------------------------- #
# Provisioning (fake supabase admin client)
# --------------------------------------------------------------------------- #
class _FakeAdmin:
    def __init__(self, app_metadata=None, user_metadata=None, update_error=None):
        self.app_metadata = app_metadata or {}
        self.user_metadata = user_metadata or {}
        self.update_error = update_error
        self.created: list[dict] = []
        self.updates: list[tuple] = []

    def create_user(self, attrs):
        self.created.append(attrs)
        return types.SimpleNamespace(user=types.SimpleNamespace(id=U1))

    def get_user_by_id(self, uid):
        return types.SimpleNamespace(user=types.SimpleNamespace(
            app_metadata=self.app_metadata, user_metadata=self.user_metadata))

    def update_user_by_id(self, uid, attrs):
        if self.update_error:
            raise self.update_error
        self.updates.append((uid, attrs))


class _FakeTable:
    def __init__(self):
        self.rows: list[dict] = []

    def insert(self, row):
        self.rows.append(row)
        return self

    def update(self, patch):
        return self

    def eq(self, *a):
        return self

    def execute(self):
        return types.SimpleNamespace(data=[])


class _FakeClient:
    def __init__(self, admin):
        self.auth = types.SimpleNamespace(admin=admin)
        self.profiles = _FakeTable()

    def table(self, name):
        return self.profiles


def _patch_client(monkeypatch, admin):
    client = _FakeClient(admin)
    monkeypatch.setattr(prov, "service_client", lambda: client)
    return client


def test_temp_password_has_digit_and_length():
    for _ in range(50):
        pw = prov._temp_password()
        assert len(pw) >= 14 and any(c.isdigit() for c in pw)


def test_provision_user_sets_flag_and_language(monkeypatch):
    admin = _FakeAdmin()
    _patch_client(monkeypatch, admin)
    prov.provision_user(email="a@b.co", roles=["merchant_owner"], merchant_id=M1,
                        name="Ana", must_change_password=True, language="en")
    attrs = admin.created[0]
    assert attrs["app_metadata"] == {
        "roles": ["merchant_owner"], "merchant_id": M1, "must_change_password": True}
    assert attrs["user_metadata"] == {"language": "en"}


def test_provision_user_defaults_unchanged(monkeypatch):
    admin = _FakeAdmin()
    _patch_client(monkeypatch, admin)
    prov.provision_user(email="a@b.co", roles=["operation_user"], merchant_id=M1,
                        station="caja_1")
    attrs = admin.created[0]
    assert "must_change_password" not in attrs["app_metadata"]
    assert "user_metadata" not in attrs


def test_issue_temp_password_single_merged_write(monkeypatch):
    admin = _FakeAdmin(app_metadata={"roles": ["merchant_owner"], "merchant_id": M1})
    _patch_client(monkeypatch, admin)
    pw = prov.issue_temp_password(U1)
    assert len(admin.updates) == 1
    uid, attrs = admin.updates[0]
    assert uid == U1 and attrs["password"] == pw
    assert attrs["app_metadata"] == {
        "roles": ["merchant_owner"], "merchant_id": M1, "must_change_password": True}


def test_complete_password_change_single_write_clears_flag(monkeypatch):
    admin = _FakeAdmin(app_metadata={"roles": ["merchant_owner"], "merchant_id": M1,
                                     "must_change_password": True})
    _patch_client(monkeypatch, admin)
    prov.complete_password_change(U1, "new-password-1")
    assert len(admin.updates) == 1
    _, attrs = admin.updates[0]
    assert attrs["password"] == "new-password-1"
    assert attrs["app_metadata"]["must_change_password"] is False
    assert attrs["app_metadata"]["roles"] == ["merchant_owner"]


_OWNER_META = {"roles": ["merchant_owner"], "merchant_id": M1}


@pytest.mark.parametrize("exc,fragment", [
    (AuthWeakPasswordError("weak", 422, ["length"]), "too weak"),
    (AuthApiError("weak", 422, "weak_password"), "too weak"),
    (AuthApiError("same", 422, "same_password"), "differ"),
])
def test_complete_password_change_maps_to_validation(monkeypatch, exc, fragment):
    _patch_client(monkeypatch, _FakeAdmin(app_metadata=_OWNER_META, update_error=exc))
    with pytest.raises(ValidationFailed) as ei:
        prov.complete_password_change(U1, "whatever-1")
    assert fragment in ei.value.message
    expected = "same_password" if fragment == "differ" else "weak_password"
    assert ei.value.details["reason"] == expected
    if isinstance(exc, AuthWeakPasswordError):
        assert ei.value.details["reasons"] == ["length"]


def test_complete_password_change_other_auth_error_is_conflict(monkeypatch):
    from conpass_common.errors import Conflict
    err = AuthApiError("x", 500, "unexpected_failure")
    _patch_client(monkeypatch, _FakeAdmin(app_metadata=_OWNER_META, update_error=err))
    with pytest.raises(Conflict):
        prov.complete_password_change(U1, "whatever-1")


def test_merge_refuses_to_write_without_current_roles(monkeypatch):
    """An unreadable/empty app_metadata must never be overwritten with just the patch —
    that would strip roles + merchant_id from the login."""
    from conpass_common.errors import Conflict
    admin = _FakeAdmin(app_metadata={})
    _patch_client(monkeypatch, admin)
    with pytest.raises(Conflict):
        prov.issue_temp_password(U1)
    with pytest.raises(Conflict):
        prov.complete_password_change(U1, "whatever-1")
    assert admin.updates == []


def test_update_user_station_is_merge_safe(monkeypatch):
    admin = _FakeAdmin(app_metadata={"roles": ["operation_user"], "merchant_id": M1})
    _patch_client(monkeypatch, admin)
    prov.update_user(U1, station="caja_2")
    assert admin.updates[0][1]["app_metadata"] == {
        "roles": ["operation_user"], "merchant_id": M1, "station": "caja_2"}


@pytest.mark.parametrize("meta,expected", [
    ({"language": "en"}, "en"), ({"language": "fr"}, "es"), ({}, "es")])
def test_get_user_language(monkeypatch, meta, expected):
    _patch_client(monkeypatch, _FakeAdmin(user_metadata=meta))
    assert prov.get_user_language(U1) == expected


# --------------------------------------------------------------------------- #
# Auth claims + identity service
# --------------------------------------------------------------------------- #
def test_claims_parse_must_change_password():
    on = auth_mod._claims_to_identity(
        {"sub": U1, "app_metadata": {"roles": ["merchant_owner"], "must_change_password": True}})
    off = auth_mod._claims_to_identity({"sub": U1, "app_metadata": {"roles": ["x"]}})
    assert on.must_change_password is True and off.must_change_password is False


def test_me_includes_must_change_password(monkeypatch):
    import services.identity.handler as h
    monkeypatch.setitem(h.app.dependency_overrides, require_identity, lambda: Identity(
        user_id=U1, email="a@b.co", roles=["merchant_owner"], must_change_password=True))
    assert TestClient(h.app).get("/me").json()["mustChangePassword"] is True
    _identity(h.app, monkeypatch)
    assert TestClient(h.app).get("/me").json()["mustChangePassword"] is False


def test_change_password_204(monkeypatch):
    import services.identity.handler as h
    calls = []
    monkeypatch.setattr(h, "complete_password_change",
                        lambda uid, pw, email=None: calls.append((uid, pw)))
    _identity(h.app, monkeypatch, roles=("merchant_owner",))
    resp = TestClient(h.app).post("/me/password", json={"newPassword": "brand-new-pw1"})
    assert resp.status_code == 204 and calls == [(U1, "brand-new-pw1")]


def test_change_password_demo_owner_forbidden(monkeypatch):
    import services.identity.handler as h
    calls = []
    monkeypatch.setattr(h, "complete_password_change",
                        lambda uid, pw, email=None: calls.append(pw))
    _identity(h.app, monkeypatch, roles=("merchant_owner",), email=DEMO_OWNER_EMAIL.upper())
    resp = TestClient(h.app).post("/me/password", json={"newPassword": "brand-new-pw1"})
    assert resp.status_code == 403 and calls == []


def test_change_password_too_short_422(monkeypatch):
    import services.identity.handler as h
    _identity(h.app, monkeypatch, roles=("merchant_owner",))
    resp = TestClient(h.app).post("/me/password", json={"newPassword": "Qz7xK"})
    assert resp.status_code == 422
    body = resp.json()
    # Same Error shape as ValidationFailed, with a reason the PWA can translate —
    # and pydantic's `input` (the submitted password) is never echoed back.
    assert body["code"] == "validation_error"
    assert body["details"]["reason"] == "invalid_fields"
    assert body["details"]["fields"][0]["field"] == "newPassword"
    assert "Qz7xK" not in resp.text


def test_change_password_weak_returns_reason_and_reasons(monkeypatch):
    import services.identity.handler as h

    def weak(uid, pw, email=None):
        raise ValidationFailed("password too weak",
                               details={"reason": "weak_password", "reasons": ["characters"]})
    monkeypatch.setattr(h, "complete_password_change", weak)
    _identity(h.app, monkeypatch, roles=("merchant_owner",))
    resp = TestClient(h.app).post("/me/password", json={"newPassword": "brand-new-pw1"})
    assert resp.status_code == 422
    assert resp.json()["details"] == {"reason": "weak_password", "reasons": ["characters"]}


# --------------------------------------------------------------------------- #
# Onboarding (POST /merchants)
# --------------------------------------------------------------------------- #
class _Sender:
    def __init__(self, configured=True, error=None):
        self.configured = configured
        self.error = error
        self.sent: list[tuple] = []

    def send(self, to, subject, text, html=None):
        if self.error:
            raise self.error
        self.sent.append((to, subject, text, html))


def _onboard_setup(monkeypatch, sender):
    from tests.test_payment_settings import _onboard_body, _seed_merchants
    h, repo = _seed_merchants(monkeypatch)
    provisioned = {}

    def _prov(**kw):
        provisioned.update(kw)
        return types.SimpleNamespace(user_id="owner-1", email=kw["email"], temp_password=PW)
    monkeypatch.setattr(h, "get_payment_provider",
                        lambda: types.SimpleNamespace(submit=lambda intent: None))
    monkeypatch.setattr(h, "provision_user", _prov)
    monkeypatch.setattr(h, "get_email_sender", lambda: sender)
    return h, provisioned, _onboard_body(method="card")


def test_onboard_sends_email_and_hides_password(monkeypatch):
    sender = _Sender()
    h, provisioned, body = _onboard_setup(monkeypatch, sender)
    body["language"] = "en"
    resp = TestClient(h.app).post("/merchants", json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["ownerInviteSent"] is True and data["ownerEmail"] == "ana@test.co"
    assert "ownerTempPassword" not in data and PW not in resp.text
    assert provisioned["must_change_password"] is True and provisioned["language"] == "en"
    to, subject, text, html = sender.sent[0]
    assert to == "ana@test.co" and PW in text and PW in html
    assert "ready" in subject.lower()               # English template


def test_onboard_defaults_to_spanish(monkeypatch):
    sender = _Sender()
    h, provisioned, body = _onboard_setup(monkeypatch, sender)
    assert TestClient(h.app).post("/merchants", json=body).status_code == 201
    assert provisioned["language"] == "es" and "Deberá" in sender.sent[0][2]


def test_onboard_email_failure_still_201(monkeypatch, caplog):
    sender = _Sender(error=EmailDeliveryError("SMTPAuthenticationError"))
    h, _, body = _onboard_setup(monkeypatch, sender)
    resp = TestClient(h.app).post("/merchants", json=body)
    assert resp.status_code == 201
    assert resp.json()["ownerInviteSent"] is False
    assert "ownerTempPassword" not in resp.json()
    assert PW not in caplog.text


# --------------------------------------------------------------------------- #
# Admin resend credentials
# --------------------------------------------------------------------------- #
class _AdminRepo:
    def __init__(self, merchant=None, owner=None):
        self.merchant, self.owner = merchant, owner

    def get_merchant(self, mid):
        return self.merchant

    def owner_profile(self, mid):
        return self.owner


MERCHANT = {"id": M1, "business_name": "Café Sol", "contact_name": "Ana", "is_demo": False}
OWNER = {"user_id": "owner-1", "email": "ana@test.co", "name": "Ana"}
URL = f"/admin/clients/{M1}/resend-credentials"


def _resend_setup(monkeypatch, *, merchant=MERCHANT, owner=OWNER, sender=None,
                  roles=("platform_admin",)):
    import services.admin.handler as h
    sender = sender or _Sender()
    issued: list[str] = []
    monkeypatch.setattr(h, "get_repo", lambda: _AdminRepo(merchant, owner))
    monkeypatch.setattr(h, "get_email_sender", lambda: sender)
    monkeypatch.setattr(h, "get_user_language", lambda uid: "en")
    monkeypatch.setattr(h, "issue_temp_password", lambda uid: issued.append(uid) or PW)
    _identity(h.app, monkeypatch, roles=roles)
    return h, sender, issued


def test_resend_happy_path(monkeypatch):
    h, sender, issued = _resend_setup(monkeypatch)
    resp = TestClient(h.app).post(URL)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"sent": True, "email": "ana@test.co"}
    assert issued == ["owner-1"]
    to, subject, text, html = sender.sent[0]
    assert to == "ana@test.co" and PW in text and "new" in subject.lower()


def test_resend_demo_merchant_409(monkeypatch):
    h, sender, issued = _resend_setup(monkeypatch, merchant={**MERCHANT, "is_demo": True})
    assert TestClient(h.app).post(URL).status_code == 409
    assert issued == [] and sender.sent == []


def test_resend_missing_merchant_404(monkeypatch):
    h, _, issued = _resend_setup(monkeypatch, merchant=None)
    assert TestClient(h.app).post(URL).status_code == 404 and issued == []


def test_resend_no_owner_404(monkeypatch):
    h, _, issued = _resend_setup(monkeypatch, owner=None)
    assert TestClient(h.app).post(URL).status_code == 404 and issued == []


def test_resend_not_configured_503_before_password_reset(monkeypatch):
    h, _, issued = _resend_setup(monkeypatch, sender=_Sender(configured=False))
    resp = TestClient(h.app).post(URL)
    assert resp.status_code == 503 and issued == []


def test_resend_send_failure_502_after_reset(monkeypatch):
    h, _, issued = _resend_setup(
        monkeypatch, sender=_Sender(error=EmailDeliveryError("SMTPException")))
    resp = TestClient(h.app).post(URL)
    assert resp.status_code == 502
    assert resp.json()["code"] == "email_delivery_failed"
    assert issued == ["owner-1"]


def test_resend_requires_platform_admin(monkeypatch):
    h, _, issued = _resend_setup(monkeypatch, roles=("merchant_owner",))
    assert TestClient(h.app).post(URL).status_code == 403 and issued == []


def test_complete_password_change_refuses_current_password(monkeypatch):
    """The admin API happily re-sets the same password, so reusing the emailed temporary
    password is caught by probing it first — and nothing is written."""
    admin = _FakeAdmin(app_metadata=_OWNER_META)
    _patch_client(monkeypatch, admin)
    monkeypatch.setattr(prov, "is_current_password", lambda email, pw: True)
    with pytest.raises(ValidationFailed) as ei:
        prov.complete_password_change(U1, "temp-pw-123", email="a@b.co")
    assert ei.value.details == {"reason": "same_password"} and admin.updates == []


def test_complete_password_change_new_password_passes_probe(monkeypatch):
    admin = _FakeAdmin(app_metadata=_OWNER_META)
    _patch_client(monkeypatch, admin)
    monkeypatch.setattr(prov, "is_current_password", lambda email, pw: False)
    prov.complete_password_change(U1, "brand-new-pw1", email="a@b.co")
    assert len(admin.updates) == 1


def test_is_current_password_probe(monkeypatch):
    import httpx
    from conpass_common.config import settings
    monkeypatch.setattr(type(settings), "supabase_url", property(lambda s: "https://x.supabase.co"))
    monkeypatch.setattr(type(settings), "supabase_publishable_key", property(lambda s: "pk"))
    seen = {}

    def fake_post(url, **kw):
        seen["url"] = url
        return types.SimpleNamespace(status_code=kw["json"]["password"] == "cur" and 200 or 400)
    monkeypatch.setattr(httpx, "post", fake_post)
    assert prov.is_current_password("a@b.co", "cur") is True
    assert prov.is_current_password("a@b.co", "other") is False
    assert "grant_type=password" in seen["url"]

    def boom(url, **kw):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(httpx, "post", boom)
    assert prov.is_current_password("a@b.co", "cur") is False
