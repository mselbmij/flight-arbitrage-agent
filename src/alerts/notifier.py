"""ARCHITECTURE.md section 7a: ship an `Alert` to an ntfy topic.

The notifier never composes a message (`rules.py` does): it splits `Alert.message` into a title
line and a body, maps the alert kind onto a priority and tags, and POSTs it. One request per
alert, no retries (the next run retries undelivered alerts), the token is never logged.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from src.models import Alert, AlertKind

log = logging.getLogger(__name__)

#: AlertKind -> (ntfy priority, ntfy tags). ARCHITECTURE.md section 7a.
NTFY_MAPPING: dict[AlertKind, tuple[int, str]] = {
    AlertKind.BUY: (5, "airplane,moneybag"),
    AlertKind.EXTREME_LOW: (4, "airplane,eyes"),
    AlertKind.FENCE_WARNING: (3, "hourglass"),
    AlertKind.CREDIT_DROP: (4, "airplane,receipt"),
    AlertKind.DOT_24H: (4, "alarm_clock"),
    AlertKind.BUDGET_TRIPPED: (3, "warning"),
    AlertKind.PROVIDER_ERROR: (2, "x"),
}

_LOGGED_BODY_CHARS = 200
_ASCII_FOR = {"→": "->", "—": "-", "–": "-"}  # arrows and dashes the rule titles use


def _latin1(text: str) -> str:
    """HTTP header values must be latin-1: transliterate the arrow and dashes, drop the rest."""
    for char, ascii_ in _ASCII_FOR.items():
        text = text.replace(char, ascii_)
    return text.encode("latin-1", errors="ignore").decode("latin-1")


def split_message(message: str) -> tuple[str, str]:
    """(title, body): the first line and everything after it; a one-line message is both."""
    title, sep, body = message.partition("\n")
    return title, (body if sep and body else title)


class NtfyNotifier:
    def __init__(
        self,
        server: str,
        topic: str,
        token: str | None = None,
        *,
        client: httpx.Client | None = None,
        timeout: float = 10,
    ) -> None:
        self.url = f"{server.rstrip('/')}/{quote(topic, safe='')}"
        self._token = token or None
        self._client = client
        self.timeout = timeout

    def send(
        self, alert: Alert, *, click: str | None = None, priority: int | None = None
    ) -> str | None:
        """POST the alert. "ntfy" on a 2xx, None on any other outcome. `priority` overrides the
        kind's mapping (operational pings that need a fix, not stored alerts)."""
        title, body = split_message(alert.message)
        mapped, tags = NTFY_MAPPING[alert.kind]
        priority = priority or mapped
        headers = {
            "Title": _latin1(title).replace("\r", "").encode("latin-1"),
            "Priority": str(priority).encode(),
            "Tags": tags.encode(),
        }
        if click:
            headers["Click"] = _latin1(click).encode("latin-1")
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}".encode("latin-1", errors="ignore")
        try:
            payload = body.encode("utf-8")
        except UnicodeEncodeError:  # lone surrogates only
            payload = _latin1(body).encode("utf-8")

        try:
            if self._client is not None:
                resp = self._client.post(
                    self.url, content=payload, headers=headers, timeout=self.timeout
                )
            else:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(self.url, content=payload, headers=headers)
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            log.warning("ntfy: %s while posting alert %s", type(e).__name__, alert.alert_key)
            return None
        if not resp.is_success:
            log.warning("ntfy: HTTP %d: %s", resp.status_code, resp.text[:_LOGGED_BODY_CHARS])
            return None
        return "ntfy"


class LogNotifier:
    """`--notifier log`: print the title; nothing leaves the machine."""

    def send(
        self, alert: Alert, *, click: str | None = None, priority: int | None = None
    ) -> str | None:
        print(split_message(alert.message)[0])
        return "log"
