"""Phase 6: src/alerts/notifier.py (ARCHITECTURE.md section 7a). respx only, no network."""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from src.alerts.notifier import NTFY_MAPPING, LogNotifier, NtfyNotifier, split_message
from src.models import AlertKind
from tests.conftest import make_alert

SERVER = "https://ntfy.example.test"
TOPIC = "fares-test"
URL = f"{SERVER}/{TOPIC}"
TOKEN = "tk_supersecret123"
MSG = "BUY SEA→SJC $287 (DTD 34, prime)\nAS 327 · 1 stop\n$287 vs typical-low $300 × 1.00"


def notifier(**kw) -> NtfyNotifier:
    return NtfyNotifier(SERVER, TOPIC, **kw)


def sent(route: respx.Route) -> httpx.Request:
    assert route.call_count == 1
    return route.calls.last.request


@pytest.fixture
def mock():
    with respx.mock(assert_all_called=False) as m:
        yield m


# -- request shape ------------------------------------------------------------------------ #


def test_posts_to_server_slash_topic_and_returns_ntfy_on_2xx(mock):
    route = mock.post(URL).respond(200, json={"id": "x"})
    assert notifier().send(make_alert(message=MSG)) == "ntfy"
    assert sent(route).method == "POST"


def test_trailing_slash_on_the_server_is_tolerated(mock):
    route = mock.post(URL).respond(200)
    assert NtfyNotifier(SERVER + "/", TOPIC).send(make_alert(message=MSG)) == "ntfy"
    assert route.called


def test_body_is_everything_after_the_first_line_and_title_is_the_first_line(mock):
    route = mock.post(URL).respond(200)
    notifier().send(make_alert(message=MSG))
    req = sent(route)
    assert req.content.decode() == "AS 327 · 1 stop\n$287 vs typical-low $300 × 1.00"
    assert req.headers["Title"] == "BUY SEA->SJC $287 (DTD 34, prime)"


def test_a_message_without_a_newline_uses_the_title_as_the_body(mock):
    route = mock.post(URL).respond(200)
    notifier().send(make_alert(message="API budget tripped (4/4 day)"))
    req = sent(route)
    assert req.content.decode() == "API budget tripped (4/4 day)"
    assert req.headers["Title"] == "API budget tripped (4/4 day)"


def test_split_message():
    assert split_message("a\nb\nc") == ("a", "b\nc")
    assert split_message("only") == ("only", "only")
    assert split_message("a\n") == ("a", "a")


@pytest.mark.parametrize(
    ("kind", "priority", "tags"),
    [
        (AlertKind.BUY, "5", "airplane,moneybag"),
        (AlertKind.EXTREME_LOW, "4", "airplane,eyes"),
        (AlertKind.FENCE_WARNING, "3", "hourglass"),
        (AlertKind.CREDIT_DROP, "4", "airplane,receipt"),
        (AlertKind.DOT_24H, "4", "alarm_clock"),
        (AlertKind.BUDGET_TRIPPED, "3", "warning"),
        (AlertKind.PROVIDER_ERROR, "2", "x"),
    ],
)
def test_priority_and_tags_follow_the_section_7a_table(mock, kind, priority, tags):
    route = mock.post(URL).respond(200)
    notifier().send(make_alert(kind=kind, message=MSG))
    req = sent(route)
    assert (req.headers["Priority"], req.headers["Tags"]) == (priority, tags)


def test_every_alert_kind_has_a_mapping():
    assert set(NTFY_MAPPING) == set(AlertKind)


def test_click_header_only_when_a_click_url_is_given(mock):
    route = mock.post(URL).respond(200)
    notifier().send(make_alert(message=MSG), click="https://flights.example/book?x=1")
    assert sent(route).headers["Click"] == "https://flights.example/book?x=1"
    route.reset()
    notifier().send(make_alert(message=MSG))
    assert "Click" not in sent(route).headers
    route.reset()
    notifier().send(make_alert(message=MSG), click=None)
    assert "Click" not in sent(route).headers


def test_authorization_header_only_when_a_token_is_set(mock):
    route = mock.post(URL).respond(200)
    notifier(token=TOKEN).send(make_alert(message=MSG))
    assert sent(route).headers["Authorization"] == f"Bearer {TOKEN}"
    route.reset()
    notifier().send(make_alert(message=MSG))
    assert "Authorization" not in sent(route).headers
    route.reset()
    notifier(token="").send(make_alert(message=MSG))
    assert "Authorization" not in sent(route).headers


def test_no_markdown_header_ever(mock):
    route = mock.post(URL).respond(200)
    notifier(token=TOKEN).send(make_alert(message=MSG), click="https://x.example/")
    assert "Markdown" not in sent(route).headers


# -- encoding ----------------------------------------------------------------------------- #


def test_arrow_becomes_ascii_in_the_title(mock):
    route = mock.post(URL).respond(200)
    notifier().send(make_alert(message="Re-fare SEA→JFK: now $368\nbody"))
    assert sent(route).headers["Title"] == "Re-fare SEA->JFK: now $368"


def test_other_non_latin1_characters_are_stripped_from_the_title_and_latin1_kept(mock):
    route = mock.post(URL).respond(200)
    notifier().send(make_alert(message="Café ✈ deal 日本 · $200 — soon\nbody"))
    title = sent(route).headers["Title"]
    assert "✈" not in title and "日本" not in title
    assert title.startswith("Café") and "$200" in title and "soon" in title


def test_the_body_stays_utf8(mock):
    route = mock.post(URL).respond(200)
    notifier().send(make_alert(message="t\nSEA→SJC ✈ 日本 $287"))
    assert sent(route).content.decode("utf-8") == "SEA→SJC ✈ 日本 $287"


def test_the_fence_title_from_the_rules_engine_survives_the_header_encoding(mock):
    route = mock.post(URL).respond(200)
    notifier().send(make_alert(message="21-day fence SEA→SJC — buy window closing\nx"))
    assert sent(route).headers["Title"] == "21-day fence SEA->SJC - buy window closing"


# -- outcomes ----------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [200, 201, 204])
def test_any_2xx_is_delivered(mock, status):
    mock.post(URL).respond(status)
    assert notifier().send(make_alert(message=MSG)) == "ntfy"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_non_2xx_is_not_delivered_and_is_logged_at_warning(mock, caplog, status):
    mock.post(URL).respond(status, text="x" * 500)
    with caplog.at_level(logging.WARNING, logger="src.alerts.notifier"):
        assert notifier().send(make_alert(message=MSG)) is None
    [rec] = caplog.records
    assert rec.levelno == logging.WARNING
    assert str(status) in rec.getMessage()
    assert "x" * 200 in rec.getMessage() and "x" * 201 not in rec.getMessage()


def test_a_timeout_is_not_delivered_and_not_raised(mock, caplog):
    mock.post(URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    with caplog.at_level(logging.WARNING):
        assert notifier().send(make_alert(message=MSG)) is None
    assert any("ConnectTimeout" in r.getMessage() for r in caplog.records)


def test_a_transport_error_is_not_delivered_and_not_raised(mock):
    mock.post(URL).mock(side_effect=httpx.ConnectError("refused"))
    assert notifier().send(make_alert(message=MSG)) is None


def test_no_retries_inside_send(mock):
    route = mock.post(URL).respond(503)
    notifier().send(make_alert(message=MSG))
    assert route.call_count == 1


def test_the_token_never_reaches_the_logs(mock, caplog):
    route = mock.post(URL).respond(401, text="unauthorized")
    with caplog.at_level(logging.DEBUG):
        assert notifier(token=TOKEN).send(make_alert(message=MSG)) is None
        n_http = len(caplog.records)
        route.mock(side_effect=httpx.ConnectError("refused"))
        assert notifier(token=TOKEN).send(make_alert(message=MSG)) is None
    assert n_http >= 1 and len(caplog.records) > n_http
    assert TOKEN not in caplog.text


def test_an_injected_client_is_used(mock):
    route = mock.post(URL).respond(200)
    with httpx.Client() as client:
        assert notifier(client=client).send(make_alert(message=MSG)) == "ntfy"
    assert route.called


def test_default_timeout_is_ten_seconds_and_configurable():
    assert notifier().timeout == 10
    assert notifier(timeout=3).timeout == 3


# -- LogNotifier -------------------------------------------------------------------------- #


def test_log_notifier_prints_the_title_and_reports_log(capsys):
    assert LogNotifier().send(make_alert(message=MSG)) == "log"
    assert capsys.readouterr().out == "BUY SEA→SJC $287 (DTD 34, prime)\n"
