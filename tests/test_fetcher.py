"""Test HTTP policy and pagination through mocks without contacting the portal."""

from unittest.mock import Mock

import pytest
import requests
from conftest import FIXTURES

from geospatial_web_harvester.config import FIXTURE_URL
from geospatial_web_harvester.fetcher import FetchError, HttpFetcher, fetch_pages


def test_mocked_two_page_fixture_pagination():
    pages = {
        FIXTURE_URL: (FIXTURES / "planning_basic.html").read_text(),
        FIXTURE_URL.replace("page=1", "page=2"): (
            FIXTURES / "planning_edge_cases.html"
        ).read_text(),
    }
    fetch = Mock(side_effect=pages.__getitem__)
    result = list(fetch_pages(FIXTURE_URL, fetch))
    assert [page.fetched_cards for page in result] == [8, 32]
    assert fetch.call_count == 2


@pytest.mark.parametrize("status", [401, 403, 429])
def test_restrictions_stop_immediately(status):
    session = Mock()
    session.get.return_value = Mock(status_code=status)
    with pytest.raises(FetchError, match=str(status)):
        HttpFetcher(session=session, sleep=Mock()).fetch("https://example.com/source")
    assert session.get.call_count == 1


def test_bounded_server_retries_and_timeout():
    session = Mock()
    session.get.side_effect = [
        Mock(status_code=500),
        requests.Timeout(),
        Mock(
            status_code=200,
            text="<html>ok</html>",
            headers={"Content-Type": "text/html"},
        ),
    ]
    sleeper = Mock()
    fetcher = HttpFetcher(session=session, sleep=sleeper)
    assert fetcher.fetch("https://example.com/source") == "<html>ok</html>"
    assert session.get.call_count == 3
    assert sleeper.call_count == 2
    assert session.get.call_args.kwargs["timeout"] == 20
    assert session.get.call_args.kwargs["allow_redirects"] is False
    session.get.reset_mock(side_effect=True)
    session.get.side_effect = requests.Timeout()
    with pytest.raises(FetchError, match="timeout"):
        fetcher.fetch("https://example.com/source")
    assert session.get.call_count == 3


def test_no_real_fixture_requests():
    session = Mock()
    with pytest.raises(FetchError, match="Reserved"):
        HttpFetcher(session=session).fetch(FIXTURE_URL)
    session.get.assert_not_called()


def test_empty_malformed_cycle_page_limit_and_origin():
    assert (
        list(fetch_pages(FIXTURE_URL, lambda _: '<main class="notice-list"></main>'))[
            0
        ].fetched_cards
        == 0
    )
    with pytest.raises(ValueError, match="Unrecognized"):
        list(fetch_pages(FIXTURE_URL, lambda _: "Service unavailable"))
    with pytest.raises(FetchError, match="Empty"):
        HttpFetcher(
            session=Mock(
                get=Mock(
                    return_value=Mock(
                        status_code=200, text="", headers={"Content-Type": "text/html"}
                    )
                )
            )
        ).fetch("https://example.com/source")
    html = '<main class="notice-list"></main><a rel="next" href="{url}">Next</a>'
    with pytest.raises(FetchError, match="cycle"):
        list(fetch_pages(FIXTURE_URL, lambda _: html.format(url=FIXTURE_URL)))
    with pytest.raises(FetchError, match="limit"):
        list(
            fetch_pages(FIXTURE_URL, lambda _: html.format(url="?page=2"), max_pages=1)
        )
    with pytest.raises(FetchError, match="origin"):
        list(
            fetch_pages(
                FIXTURE_URL, lambda _: html.format(url="https://other.invalid/page")
            )
        )


def test_failed_second_page_cannot_produce_successful_batch():
    fetch = Mock(
        side_effect=[
            (FIXTURES / "planning_basic.html").read_text(),
            FetchError("timeout"),
        ]
    )
    persist = Mock()
    with pytest.raises(FetchError):
        pages = list(fetch_pages(FIXTURE_URL, fetch))
        persist(pages)
    persist.assert_not_called()


def test_non_html_response_is_rejected():
    session = Mock()
    session.get.return_value = Mock(
        status_code=200,
        text='{"data":[]}',
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(FetchError, match="HTML"):
        HttpFetcher(session=session).fetch("https://example.com/source")
