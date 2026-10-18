"""Amadeus request building, OAuth token caching, and HTTP error mapping.

ARCHITECTURE section 6: clients never retry a provider call. The single permitted re-send is the
401 -> refresh token -> retry once, which is authentication, not a provider retry.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from src.ingestion.amadeus_client import AmadeusProvider, build_params
from src.ingestion.base import ProviderError
from src.models import Cabin, Constraints, Provider, ResolvedWatchlist, SearchTarget
from tests.conftest import T0, Clock, thanksgiving

FIX = Path(__file__).parent / "fixtures" / "amadeus"
TEST_HOST, PROD_HOST = "https://test.api.amadeus.com", "https://api.amadeus.com"
TOKEN_PATH, SEARCH_PATH = "/v1/security/oauth2/token", "/v2/shopping/flight-offers"
CID, SECRET = "fixture-client-id", "fixture-client-secret"

RT_EXPECTED = {
    "originLocationCode": "SEA",
    "destinationLocationCode": "SJC",
    "departureDate": "2026-11-25",
    "returnDate": "2026-11-29",
    "adults": "1",
    "travelClass": "ECONOMY",
    "currencyCode": "USD",
    "max": "20",
}


def jload(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def wl_with(constraints: Constraints | None = None, **top) -> ResolvedWatchlist:
    update = dict(top)
    if constraints is not None:
        update["constraints"] = constraints
    return thanksgiving().model_copy(update=update)


def params_for(wl: ResolvedWatchlist | None = None, target: SearchTarget | None = None) -> dict:
    wl = wl or thanksgiving()
    return build_params(target or wl.targets[0], wl)


# --------------------------------------------------------------------------- build_params


def test_round_trip_params_are_exactly_the_documented_set():
    assert params_for() == RT_EXPECTED


def test_all_param_values_are_strings():
    assert all(isinstance(v, str) for v in params_for().values())


def test_one_way_omits_return_date():
    one_way = SearchTarget(watchlist_id="sea-sjc-thanksgiving", origin="SEA", destination="SJC",
                           depart=date(2026, 11, 25))  # fmt: skip
    p = params_for(target=one_way)
    assert "returnDate" not in p
    assert p == {k: v for k, v in RT_EXPECTED.items() if k != "returnDate"}


@pytest.mark.parametrize(
    ("cabin", "code"),
    [
        (Cabin.ECONOMY, "ECONOMY"),
        (Cabin.PREMIUM_ECONOMY, "PREMIUM_ECONOMY"),
        (Cabin.BUSINESS, "BUSINESS"),
        (Cabin.FIRST, "FIRST"),
    ],
)
def test_travel_class_mapping(cabin, code):
    assert params_for(wl_with(cabin=cabin))["travelClass"] == code


def test_non_stop_true_only_when_max_stops_is_zero():
    assert params_for(wl_with(Constraints(max_stops=0)))["nonStop"] == "true"


@pytest.mark.parametrize("max_stops", [1, 2, 3])
def test_non_stop_omitted_when_stops_are_allowed(max_stops):
    assert "nonStop" not in params_for(wl_with(Constraints(max_stops=max_stops)))


def test_passengers_and_currency_follow_the_watchlist():
    p = params_for(wl_with(passengers=2, currency="EUR"))
    assert (p["adults"], p["currencyCode"]) == ("2", "EUR")


def test_included_airline_codes_joined_with_commas():
    p = params_for(wl_with(Constraints(allowed_carriers=["AS", "UA"])))
    assert p["includedAirlineCodes"] == "AS,UA"
    assert "excludedAirlineCodes" not in p


def test_excluded_airline_codes_joined_with_commas():
    p = params_for(wl_with(Constraints(excluded_carriers=["NK", "F9"])))
    assert p["excludedAirlineCodes"] == "NK,F9"
    assert "includedAirlineCodes" not in p


@pytest.mark.parametrize(
    ("allowed", "excluded"),
    [([], []), (["AS"], []), ([], ["NK"]), (["AS", "UA"], ["NK", "F9"])],
)
def test_included_and_excluded_airline_codes_are_never_both_sent(allowed, excluded):
    p = params_for(wl_with(Constraints(allowed_carriers=allowed, excluded_carriers=excluded)))
    assert not ({"includedAirlineCodes", "excludedAirlineCodes"} <= p.keys())


# --------------------------------------------------------------------------- harness


def token_json(token: str = "tok-one", expires_in: int = 1799) -> dict:
    return {**jload("amadeus_token.json"), "access_token": token, "expires_in": expires_in}


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as r:
        yield r


@pytest.fixture
def clock() -> Clock:
    return Clock(T0)


def make(clock, host="test", **kw) -> AmadeusProvider:
    return AmadeusProvider(CID, SECRET, host=host, client=httpx.Client(), now=clock, **kw)


def routes(router, host_url=TEST_HOST, tokens=None, search=None):
    tok = router.post(f"{host_url}{TOKEN_PATH}").mock(
        side_effect=[httpx.Response(200, json=t) for t in (tokens or [token_json()])]
    )
    srch = router.get(f"{host_url}{SEARCH_PATH}").mock(
        side_effect=search or [httpx.Response(200, json=jload("amadeus_sea_sjc_branded.json"))] * 10
    )
    return tok, srch


def search(p: AmadeusProvider):
    wl = thanksgiving()
    return p.search(wl.targets[0], wl, "run-1", T0)


# --------------------------------------------------------------------------- search + provider stamp


def test_search_parses_offers_and_sends_bearer_token_and_params(router, clock):
    tok, srch = routes(router, tokens=[token_json("tok-abc")])
    res = search(make(clock))
    assert [o.price_cents for o in res.observations] == [19840, 23107, 26250, 25603]
    assert res.band is None
    req = srch.calls.last.request
    assert req.headers["Authorization"] == "Bearer tok-abc"
    assert dict(req.url.params) == RT_EXPECTED


def test_test_host_forces_provider_mock_on_every_observation(router, clock):
    routes(router)
    res = search(make(clock, host="test"))
    assert {o.provider for o in res.observations} == {Provider.MOCK}


def test_production_host_stamps_provider_amadeus(router, clock):
    tok, srch = routes(router, host_url=PROD_HOST)
    res = search(make(clock, host="production"))
    assert {o.provider for o in res.observations} == {Provider.AMADEUS}
    assert tok.call_count == 1 and srch.call_count == 1


def test_default_host_is_test(router, clock):
    routes(router)
    res = search(AmadeusProvider(CID, SECRET, client=httpx.Client(), now=clock))
    assert {o.provider for o in res.observations} == {Provider.MOCK}


def test_empty_data_response_gives_no_observations(router, clock):
    routes(router, search=[httpx.Response(200, json={"meta": {"count": 0}, "data": []})])
    assert search(make(clock)).observations == []


# --------------------------------------------------------------------------- token lifecycle


def test_token_request_is_a_client_credentials_form_post(router, clock):
    tok, _ = routes(router)
    search(make(clock))
    req = tok.calls.last.request
    assert req.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
    form = parse_qs(req.content.decode())
    assert form == {
        "grant_type": ["client_credentials"],
        "client_id": [CID],
        "client_secret": [SECRET],
    }


def test_token_endpoint_is_hit_once_across_two_searches(router, clock):
    tok, srch = routes(router)
    p = make(clock)
    search(p)
    search(p)
    assert tok.call_count == 1
    assert srch.call_count == 2


def test_cached_token_is_reused_one_second_before_the_refresh_margin(router, clock):
    tok, _ = routes(router, tokens=[token_json("tok-one"), token_json("tok-two")])
    p = make(clock)
    search(p)
    clock.t = T0 + timedelta(seconds=1799 - 60 - 1)
    search(p)
    assert tok.call_count == 1


def test_token_is_refreshed_at_expires_in_minus_60_seconds(router, clock):
    tok, srch = routes(router, tokens=[token_json("tok-one"), token_json("tok-two")])
    p = make(clock)
    search(p)
    clock.t = T0 + timedelta(seconds=1799 - 60)
    search(p)
    assert tok.call_count == 2
    assert srch.calls.last.request.headers["Authorization"] == "Bearer tok-two"


def test_refresh_margin_follows_the_expires_in_the_server_sent(router, clock):
    tok, _ = routes(router, tokens=[token_json("a", expires_in=120), token_json("b")])
    p = make(clock)
    search(p)
    clock.t = T0 + timedelta(seconds=59)
    search(p)
    assert tok.call_count == 1
    clock.t = T0 + timedelta(seconds=60)
    search(p)
    assert tok.call_count == 2


def test_401_on_search_refreshes_the_token_and_retries_once(router, clock):
    ok = httpx.Response(200, json=jload("amadeus_sea_sjc_branded.json"))
    tok, srch = routes(
        router,
        tokens=[token_json("stale"), token_json("fresh")],
        search=[httpx.Response(401, json={"errors": [{"status": 401, "code": 38190}]}), ok],
    )
    res = search(make(clock))
    assert len(res.observations) == 4
    assert (tok.call_count, srch.call_count) == (2, 2)
    assert srch.calls[0].request.headers["Authorization"] == "Bearer stale"
    assert srch.calls[1].request.headers["Authorization"] == "Bearer fresh"


def test_second_401_is_a_provider_error_with_no_third_attempt(router, clock):
    unauth = httpx.Response(401, json={"errors": [{"status": 401, "code": 38190}]})
    tok, srch = routes(
        router,
        tokens=[token_json("a"), token_json("b"), token_json("c")],
        search=[unauth, unauth, unauth],
    )
    with pytest.raises(ProviderError) as exc:
        search(make(clock))
    assert exc.value.status_code == 401 and exc.value.retryable is False
    assert (tok.call_count, srch.call_count) == (2, 2)


def test_rejected_credentials_fail_before_any_search(router, clock):
    tok = router.post(f"{TEST_HOST}{TOKEN_PATH}").mock(
        return_value=httpx.Response(401, json={"error": "invalid_client"})
    )
    srch = router.get(f"{TEST_HOST}{SEARCH_PATH}").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(ProviderError) as exc:
        search(make(clock))
    assert exc.value.retryable is False
    assert (tok.call_count, srch.call_count) == (1, 0)
    assert SECRET not in str(exc.value)


# --------------------------------------------------------------------------- errors, no retries


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_429_and_5xx_are_retryable_and_not_retried(router, clock, status):
    tok, srch = routes(router, search=[httpx.Response(status, json={"errors": []})])
    with pytest.raises(ProviderError) as exc:
        search(make(clock))
    assert exc.value.retryable is True and exc.value.status_code == status
    assert (tok.call_count, srch.call_count) == (1, 1)


@pytest.mark.parametrize("status", [400, 403, 404])
def test_other_4xx_are_not_retryable_and_not_retried(router, clock, status):
    tok, srch = routes(router, search=[httpx.Response(status, json={"errors": []})])
    with pytest.raises(ProviderError) as exc:
        search(make(clock))
    assert exc.value.retryable is False and exc.value.status_code == status
    assert srch.call_count == 1


def test_400_error_body_is_surfaced_in_the_message(router, clock):
    routes(router, search=[httpx.Response(400, json=jload("amadeus_error_400.json"))])
    with pytest.raises(ProviderError) as exc:
        search(make(clock))
    assert "INVALID FORMAT" in str(exc.value) or "departureDate" in str(exc.value)


def test_200_with_errors_body_is_non_retryable(router, clock):
    routes(router, search=[httpx.Response(200, json=jload("amadeus_error_400.json"))])
    with pytest.raises(ProviderError) as exc:
        search(make(clock))
    assert exc.value.retryable is False


@pytest.mark.parametrize("timeout", [httpx.ReadTimeout, httpx.ConnectTimeout])
def test_timeout_is_retryable_and_not_retried(router, clock, timeout):
    tok, srch = routes(router, search=[timeout("slow")])
    with pytest.raises(ProviderError) as exc:
        search(make(clock))
    assert exc.value.retryable is True
    assert srch.call_count == 1


def test_non_json_200_body_is_a_provider_error(router, clock):
    routes(router, search=[httpx.Response(200, text="<html>gateway</html>")])
    with pytest.raises(ProviderError) as exc:
        search(make(clock))
    assert exc.value.retryable is False


def test_error_messages_never_contain_credentials_or_tokens(router, clock):
    routes(router, tokens=[token_json("tok-leak-check")], search=[httpx.Response(500, text="boom")])
    with pytest.raises(ProviderError) as exc:
        search(make(clock))
    msg = str(exc.value)
    assert SECRET not in msg and "tok-leak-check" not in msg


# --------------------------------------------------------------------------- non-transport httpx errors


def _decoding_error() -> httpx.DecodingError:
    return httpx.DecodingError(
        "bad gzip", request=httpx.Request("POST", f"{TEST_HOST}{TOKEN_PATH}")
    )


def test_decoding_error_on_search_is_a_non_retryable_provider_error(router, clock):
    routes(router, search=[_decoding_error()])
    with pytest.raises(ProviderError) as exc:
        search(make(clock))
    assert exc.value.retryable is False
    assert exc.value.provider is Provider.MOCK


def test_decoding_error_on_token_request_is_a_non_retryable_provider_error(router, clock):
    tok = router.post(f"{TEST_HOST}{TOKEN_PATH}").mock(side_effect=_decoding_error())
    srch = router.get(f"{TEST_HOST}{SEARCH_PATH}").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(ProviderError) as exc:
        search(make(clock))
    assert exc.value.retryable is False
    assert (tok.call_count, srch.call_count) == (1, 0)


@pytest.mark.parametrize("where", ["token", "search"])
def test_httpx_errors_do_not_chain_the_request_into_the_provider_error(router, clock, where):
    """The httpx exception carries the request: the form-encoded client secret or the bearer token."""
    if where == "token":
        router.post(f"{TEST_HOST}{TOKEN_PATH}").mock(side_effect=httpx.ConnectError("down"))
    else:
        routes(router, search=[httpx.ConnectError("down")])
    with pytest.raises(ProviderError) as exc:
        search(make(clock))
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__ is True
    assert exc.value.retryable is True  # a refused connection is a TransportError


def test_token_request_timeout_is_still_retryable(router, clock):
    router.post(f"{TEST_HOST}{TOKEN_PATH}").mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(ProviderError) as exc:
        search(make(clock))
    assert exc.value.retryable is True
