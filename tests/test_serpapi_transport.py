"""SerpAPI request building (`build_params`) and HTTP behaviour (`SerpApiProvider.search`).

ARCHITECTURE section 6: clients never retry; the runner owns retries and the ledger.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from src.ingestion.base import ProviderError
from src.ingestion.serpapi_client import SerpApiProvider, build_params
from src.models import Cabin, Constraints, Provider, ResolvedWatchlist, SearchTarget
from tests.conftest import T0, thanksgiving

FIX = Path(__file__).parent / "fixtures" / "serpapi"
KEY = "SECRETKEY123"
URL = "https://serpapi.com/search"

RT_EXPECTED = {
    "engine": "google_flights",
    "api_key": KEY,
    "departure_id": "SEA",
    "arrival_id": "SJC",
    "outbound_date": "2026-11-25",
    "return_date": "2026-11-29",
    "type": "1",
    "travel_class": "1",
    "adults": "1",
    "currency": "USD",
    "hl": "en",
    "gl": "us",
    "stops": "2",
    "exclude_basic": "true",
}


def wl_with(constraints: Constraints | None = None, **top) -> ResolvedWatchlist:
    update = dict(top)
    if constraints is not None:
        update["constraints"] = constraints
    return thanksgiving().model_copy(update=update)


def params_for(wl: ResolvedWatchlist | None = None, target: SearchTarget | None = None) -> dict:
    wl = wl or thanksgiving()
    return build_params(target or wl.targets[0], wl, KEY)


def one_way_target() -> SearchTarget:
    return SearchTarget(watchlist_id="sea-sjc-thanksgiving", origin="SEA", destination="SJC",
                        depart=date(2026, 11, 25))  # fmt: skip


# --------------------------------------------------------------------------- build_params


def test_round_trip_params_are_exactly_the_documented_set():
    assert params_for() == RT_EXPECTED


def test_all_param_values_are_strings():
    assert all(isinstance(v, str) for v in params_for().values())


def test_one_way_uses_type_2_and_omits_return_date():
    p = params_for(target=one_way_target())
    assert p["type"] == "2"
    assert "return_date" not in p
    assert p == {k: v for k, v in RT_EXPECTED.items() if k != "return_date"} | {"type": "2"}


def test_never_sends_deep_search_or_no_cache():
    p = params_for()
    assert "deep_search" not in p and "no_cache" not in p


@pytest.mark.parametrize(
    ("cabin", "code"),
    [(Cabin.ECONOMY, "1"), (Cabin.PREMIUM_ECONOMY, "2"), (Cabin.BUSINESS, "3"), (Cabin.FIRST, "4")],
)
def test_travel_class_mapping(cabin, code):
    assert params_for(wl_with(cabin=cabin))["travel_class"] == code


@pytest.mark.parametrize(("max_stops", "expected"), [(0, "1"), (1, "2"), (2, "3")])
def test_stops_mapping(max_stops, expected):
    assert params_for(wl_with(Constraints(max_stops=max_stops)))["stops"] == expected


def test_stops_omitted_when_three_stops_allowed():
    assert "stops" not in params_for(wl_with(Constraints(max_stops=3)))


def test_exclude_basic_sent_when_disallowed_in_economy():
    assert params_for()["exclude_basic"] == "true"


def test_exclude_basic_omitted_when_basic_economy_allowed():
    assert "exclude_basic" not in params_for(wl_with(Constraints(allow_basic_economy=True)))


@pytest.mark.parametrize("cabin", [Cabin.PREMIUM_ECONOMY, Cabin.BUSINESS, Cabin.FIRST])
def test_exclude_basic_omitted_outside_economy(cabin):
    assert "exclude_basic" not in params_for(wl_with(cabin=cabin))


def test_passengers_and_currency_follow_the_watchlist():
    p = params_for(wl_with(passengers=3, currency="EUR"))
    assert (p["adults"], p["currency"]) == ("3", "EUR")


def test_include_airlines_joined_with_commas():
    p = params_for(wl_with(Constraints(allowed_carriers=["AS", "UA"])))
    assert p["include_airlines"] == "AS,UA"
    assert "exclude_airlines" not in p


def test_exclude_airlines_joined_with_commas():
    p = params_for(wl_with(Constraints(excluded_carriers=["NK", "F9"])))
    assert p["exclude_airlines"] == "NK,F9"
    assert "include_airlines" not in p


def test_include_wins_when_both_lists_are_configured():
    p = params_for(wl_with(Constraints(allowed_carriers=["AS"], excluded_carriers=["DL"])))
    assert p["include_airlines"] == "AS"
    assert "exclude_airlines" not in p


@pytest.mark.parametrize(
    ("allowed", "excluded"),
    [([], []), (["AS"], []), ([], ["NK"]), (["AS", "UA"], ["NK", "F9"])],
)
def test_include_and_exclude_airlines_are_never_both_sent(allowed, excluded):
    p = params_for(wl_with(Constraints(allowed_carriers=allowed, excluded_carriers=excluded)))
    assert not ({"include_airlines", "exclude_airlines"} <= p.keys())


def test_neither_airline_param_when_no_carrier_constraints():
    p = params_for()
    assert "include_airlines" not in p and "exclude_airlines" not in p


# --------------------------------------------------------------------------- transport


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as r:
        yield r


@pytest.fixture
def provider():
    with httpx.Client() as c:
        yield SerpApiProvider(KEY, client=c)


def search(p: SerpApiProvider):
    wl = thanksgiving()
    return p.search(wl.targets[0], wl, "run-1", T0)


def payload(name: str) -> dict:
    return json.loads((FIX / name).read_text())


def test_200_is_parsed_into_observations_and_band(router, provider):
    router.get(URL).mock(
        return_value=httpx.Response(200, json=payload("serpapi_sea_sjc_rt_exclude_basic.json"))
    )
    res = search(provider)
    assert [o.price_cents for o in res.observations] == [21400, 23100, 19800, 17600]
    assert res.band is not None
    assert {o.provider for o in res.observations} == {Provider.SERPAPI}


def test_request_carries_exactly_the_expected_query_params(router, provider):
    route = router.get(URL).mock(
        return_value=httpx.Response(200, json=payload("serpapi_zero_results.json"))
    )
    search(provider)
    assert route.call_count == 1
    assert dict(route.calls.last.request.url.params) == RT_EXPECTED


def test_default_client_and_custom_base_url_are_honoured(router):
    route = router.get("https://proxy.invalid/search").mock(
        return_value=httpx.Response(200, json=payload("serpapi_zero_results.json"))
    )
    res = search(SerpApiProvider(KEY, base_url="https://proxy.invalid"))
    assert route.call_count == 1 and res.observations == []


def test_zero_results_response_is_not_an_error(router, provider):
    router.get(URL).mock(
        return_value=httpx.Response(200, json=payload("serpapi_zero_results.json"))
    )
    assert search(provider).observations == []


def test_200_with_error_body_is_non_retryable(router, provider):
    router.get(URL).mock(return_value=httpx.Response(200, json=payload("serpapi_error.json")))
    with pytest.raises(ProviderError) as exc:
        search(provider)
    assert exc.value.retryable is False and exc.value.provider is Provider.SERPAPI


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_429_and_5xx_are_retryable(router, provider, status):
    router.get(URL).mock(return_value=httpx.Response(status, json={"error": "try later"}))
    with pytest.raises(ProviderError) as exc:
        search(provider)
    assert exc.value.retryable is True
    assert exc.value.status_code == status
    assert exc.value.provider is Provider.SERPAPI


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_other_4xx_are_not_retryable(router, provider, status):
    router.get(URL).mock(return_value=httpx.Response(status, json={"error": "nope"}))
    with pytest.raises(ProviderError) as exc:
        search(provider)
    assert exc.value.retryable is False
    assert exc.value.status_code == status


@pytest.mark.parametrize("timeout", [httpx.ReadTimeout, httpx.ConnectTimeout])
def test_timeout_is_retryable(router, provider, timeout):
    router.get(URL).mock(side_effect=timeout("slow"))
    with pytest.raises(ProviderError) as exc:
        search(provider)
    assert exc.value.retryable is True


@pytest.mark.parametrize("status", [429, 500, 503])
def test_no_retry_on_retryable_http_status(router, provider, status):
    route = router.get(URL).mock(return_value=httpx.Response(status))
    with pytest.raises(ProviderError):
        search(provider)
    assert route.call_count == 1


def test_no_retry_on_timeout(router, provider):
    route = router.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError):
        search(provider)
    assert route.call_count == 1


def test_non_json_200_body_is_a_provider_error(router, provider):
    router.get(URL).mock(return_value=httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(ProviderError) as exc:
        search(provider)
    assert exc.value.retryable is False


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(403, json={"error": "denied"}),
        httpx.Response(200, json={"error": "bad"}),
    ],
)
def test_error_messages_never_contain_the_api_key(router, provider, response):
    router.get(URL).mock(return_value=response)
    with pytest.raises(ProviderError) as exc:
        search(provider)
    assert KEY not in str(exc.value)


def test_timeout_error_message_never_contains_the_api_key(router, provider):
    router.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderError) as exc:
        search(provider)
    assert KEY not in str(exc.value)
