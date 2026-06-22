from __future__ import annotations

import logging
from abc import ABC, abstractmethod


logger = logging.getLogger(__name__)


class Notifier(ABC):
    @abstractmethod
    def send(self, title: str, message: str) -> None: ...


class NullNotifier(Notifier):
    def send(self, title: str, message: str) -> None:
        logger.info("Notification: %s - %s", title, message)


class WindowsToastNotifier(Notifier):
    def send(self, title: str, message: str) -> None:
        try:
            from winotify import Notification
            Notification(app_id="XHS Local Agent", title=title, msg=message).show()
        except Exception:
            logger.exception("Windows toast notification failed")
            raise


def create_notifier(enabled: bool) -> Notifier:
    return WindowsToastNotifier() if enabled else NullNotifier()
