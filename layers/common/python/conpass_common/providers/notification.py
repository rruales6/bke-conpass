"""Notification provider abstraction (stubbed per D10).

WhatsApp (Meta/Twilio) and email (Resend/SES) drop in behind this interface later.
The stub logs and reports how many recipients *would* be notified, so reminder/birthday
flows are exercisable end-to-end offline.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Message:
    channel: str                      # push | whatsapp | email
    to: str
    template: str
    params: dict


@dataclass(frozen=True)
class SendResult:
    queued: int
    provider: str


class NotificationProvider(ABC):
    name: str

    @abstractmethod
    def send_bulk(self, messages: list[Message]) -> SendResult:
        ...


class StubNotificationProvider(NotificationProvider):
    name = "stub"

    def send_bulk(self, messages: list[Message]) -> SendResult:
        for m in messages:
            log.info("stub_notification", extra={"channel": m.channel, "to": m.to,
                                                 "template": m.template})
        return SendResult(queued=len(messages), provider=self.name)
