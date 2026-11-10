"""Provider selection from config. Feature code calls these, never a concrete class."""
from __future__ import annotations

from functools import cache, lru_cache

from ..config import settings
from .apple_wallet import AppleWalletProvider
from .email import EmailSender, SmtpEmailSender, StubEmailSender
from .google_wallet import GoogleWalletProvider
from .notification import NotificationProvider, StubNotificationProvider
from .payment import ManualProofPaymentProvider, PaymentProvider
from .wallet import WalletProvider

_WALLETS = {"google": GoogleWalletProvider, "apple": AppleWalletProvider}
_PAYMENTS = {"manual": ManualProofPaymentProvider}
_NOTIFIERS = {"stub": StubNotificationProvider}


@cache
def get_wallet_provider(kind: str | None = None) -> WalletProvider:
    return _WALLETS[kind or settings.wallet_provider]()


@lru_cache(maxsize=1)
def get_payment_provider() -> PaymentProvider:
    return _PAYMENTS[settings.payment_provider]()


@lru_cache(maxsize=1)
def get_notification_provider() -> NotificationProvider:
    return _NOTIFIERS[settings.notification_provider]()


@lru_cache(maxsize=1)
def get_email_sender() -> EmailSender:
    """Transactional email sender. `smtp` never falls back to the stub: with missing
    credentials the sender reports `configured == False` and `send` raises."""
    kind = settings.email_provider
    if kind == "stub":
        return StubEmailSender()
    if kind == "smtp":
        return SmtpEmailSender(
            host=settings.smtp_host, port=settings.smtp_port,
            username=settings.smtp_username, password=settings.smtp_password,
            from_address=settings.email_from_address, from_name=settings.email_from_name,
            reply_to=settings.email_reply_to)
    raise ValueError(f"unknown email provider: {kind!r}")
