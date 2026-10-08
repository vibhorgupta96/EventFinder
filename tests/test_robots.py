from __future__ import annotations

import time

import httpx
import pytest
from eventfinder.config import get_sources_registry
from eventfinder.robots import RobotsRules
from eventfinder.sources import (
    ROBOTS_PRODUCT_TOKEN,
    PublicPageEventSource,
    RobotsPolicy,
    SourceFetchError,
)
from eventfinder.urls import URLSafety

TOKEN = "EventFinder"
BASE = "https://events.example.test"


def _rules(text: str) -> RobotsRules:
    return RobotsRules.parse(text, TOKEN)


def _allowed(text: str, path: str) -> bool:
    return _rules(text).allows(BASE + path)


async def _public_resolver(_hostname: str) -> list[str]:
    return ["93.184.216.34"]


def _safety() -> URLSafety:
    return URLSafety(_public_resolver)


def test_product_token_is_the_user_agent_product_name():
    assert ROBOTS_PRODUCT_TOKEN == "EventFinder"


def test_multiple_star_groups_are_merged():
    text = "User-agent: *\nDisallow: /a\n\nUser-agent: *\nDisallow: /b\n"
    assert not _allowed(text, "/a") and not _allowed(text, "/b") and _allowed(text, "/c")


def test_token_groups_beat_star_and_merge():
    text = (
        "User-agent: *\nDisallow: /\n\nUser-agent: EventFinder/1.0\nDisallow: /a\n\n"
        "User-agent: eventfinder\nDisallow: /b\n"
    )
    assert not _allowed(text, "/a") and not _allowed(text, "/b") and _allowed(text, "/c")


def test_matching_empty_group_allows_everything_despite_star():
    assert _allowed("User-agent: *\nDisallow: /\n\nUser-agent: EventFinder\n", "/anything")


@pytest.mark.parametrize("agent", ["event", "finder", "EventFinderBot"])
def test_only_the_exact_product_token_matches(agent):
    text = f"User-agent: {agent}\nAllow: /\n\nUser-agent: *\nDisallow: /x\n"
    assert not _allowed(text, "/x")


def test_user_agent_lines_separated_by_blank_lines_form_one_group():
    text = "User-agent: EventFinder\n\nUser-agent: *\nDisallow: /x\n"
    assert not _allowed(text, "/x")


@pytest.mark.parametrize(
    ("rules", "path", "expected"),
    [
        ("Disallow: /*.php$", "/index.php", False),
        ("Disallow: /*.php$", "/index.php?x=1", True),
        ("Disallow: /fish*", "/fish.html", False),
        ("Disallow: /fish", "/Fish", True),
        ("Disallow: */calendar/*atom*", "/g/calendar/feed-atom", False),
        ("Disallow: /exact$", "/exact/", True),
        ("Disallow: /exact$", "/exact", False),
        ("Disallow: /a$b", "/a$b", False),
        ("Disallow: /a$b", "/a", True),
    ],
)
def test_wildcards_and_end_anchors(rules, path, expected):
    assert _allowed(f"User-agent: *\n{rules}\n", path) is expected


def test_longest_match_wins_regardless_of_order():
    text = "User-agent: *\nDisallow: /folder/\nAllow: /folder/page\n"
    assert _allowed(text, "/folder/page") and not _allowed(text, "/folder/other")
    text = "User-agent: *\nAllow: /p\nDisallow: /\n"
    assert _allowed(text, "/page") and not _allowed(text, "/q")


def test_allow_wins_an_equal_length_tie():
    assert _allowed("User-agent: *\nDisallow: /page\nAllow: /page\n", "/page")


def test_empty_rule_values_are_ignored():
    rules = _rules("User-agent: *\nDisallow:\nAllow:\n")
    assert rules.rules == () and rules.allows(BASE + "/x")


def test_robots_txt_is_always_allowed():
    assert _allowed("User-agent: *\nDisallow: /\n", "/robots.txt")


def test_query_string_participates_in_matching():
    text = "User-agent: *\nDisallow: /*?location=*\nDisallow: /*&location=*\n"
    assert not _allowed(text, "/find/?location=in--Bengaluru")
    assert not _allowed(text, "/find/?source=x&location=y")
    assert _allowed("User-agent: *\nDisallow: /*?location=*\n", "/find/?source=x&location=y")


def test_percent_encoding_is_normalized():
    assert not _allowed("User-agent: *\nDisallow: /%7Ea\n", "/~a")
    assert not _allowed("User-agent: *\nDisallow: /~a\n", "/%7ea")
    assert not _allowed("User-agent: *\nDisallow: /foo/ツ\n", "/foo/%E3%83%84")
    assert not _allowed("User-agent: *\nDisallow: /foo/%e3%83%84\n", "/foo/ツ")
    assert _allowed("User-agent: *\nDisallow: /b%2Fc\n", "/b/c")
    assert not _allowed("User-agent: *\nDisallow: /f-%2A.html\nDisallow: /f-%24\n", "/f-*.html")
    assert not _allowed("User-agent: *\nDisallow: /f-%24\n", "/f-$")


def test_robustness_against_unusual_files():
    assert _allowed("Disallow: /\nUser-agent: *\nDisallow: /x\n", "/y")
    split = "User-agent: *\nSitemap: https://e.test/s.xml\nHost: e.test\nContent-Signal: ai-train=no\nDisallow: /x\n"
    assert not _allowed(split, "/x")
    assert not _allowed("\ufeffUser-agent: *\r\nDisallow: /x # private\r\n", "/x")
    assert not _allowed("User-agent: *\nAllow: /\n\nDisallow: /apps/\n", "/apps/a")
    assert _rules("<html><body>Not robots</body></html>").rules == ()
    assert _rules('{"error": "nope"}').rules == ()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("User-agent: *\nCrawl-delay: 2.5\n", 2.5),
        ("User-agent: *\nCrawl-delay: 1\n\nUser-agent: *\nCrawl-delay: 2.5\n", 2.5),
        ("User-agent: baidu\ncrawl-delay: 1\n\n\nUser-agent: *\nDisallow: /x\n", 0.0),
        ("User-agent: eventfinder\nCrawl-delay: 3\nDisallow: /a\nUser-agent: *\nCrawl-delay: 9\n", 3.0),
        ("User-agent: *\nCrawl-delay: -1\n", 0.0),
        ("User-agent: *\nCrawl-delay: nan\n", 0.0),
        ("User-agent: *\nCrawl-delay: inf\n", 0.0),
        ("User-agent: *\nCrawl-delay: 2s\n", 0.0),
        ("User-agent: *\nAllow: /\n\n# section\nCrawl-delay: 10\nDisallow: /apps/\n", 10.0),
        # Consecutive User-agent lines are one group, so its delay applies to both agents.
        ("User-agent: eventfinder\nCrawl-delay: 3\nUser-agent: *\nCrawl-delay: 9\n", 9.0),
    ],
)
def test_crawl_delay(text, expected):
    assert _rules(text).crawl_delay == expected


def test_matching_is_linear_time():
    text = "User-agent: *\nDisallow: " + "/*a" * 40 + "b\n"
    start = time.perf_counter()
    assert _rules(text).allows(BASE + "/" + "a" * 10_000)
    assert time.perf_counter() - start < 1


MEETUP_ROBOTS = """\
Sitemap: https://www.meetup.com/pro-index-sitemap.xml
Sitemap: https://www.meetup.com/events-index-sitemap.xml

User-agent: *
Disallow: /files/
Disallow: /n/*

User-agent: *
Disallow: */events/rss/*
Disallow: */calendar/*atom*

User-agent: *
Disallow: /*?_locale=*
Disallow: /*&location=*
Disallow: /*?location=*
Disallow: /gql*
Allow: /*?_locale=

User-agent: GPTBot
Disallow: /
"""


def test_meetup_robots_denies_city_search_but_allows_group_pages():
    rules = _rules(MEETUP_ROBOTS)
    meetup = "https://www.meetup.com"
    assert not rules.allows(f"{meetup}/find/?location=in--Bengaluru")
    assert not rules.allows(f"{meetup}/find/?location=in--Bengaluru&page=2")
    assert rules.allows(f"{meetup}/bangpypers/events/")
    assert rules.allows(f"{meetup}/bangpypers/events/312819339/")
    assert not rules.allows(f"{meetup}/bangpypers/events/rss/")


def _policy(handler) -> tuple[RobotsPolicy, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RobotsPolicy(client, _safety()), client


@pytest.mark.asyncio
async def test_policy_merges_groups_and_exposes_crawl_delay():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200, text="User-agent: *\nDisallow: /a\n\nUser-agent: *\nCrawl-delay: 1.5\nDisallow: /b\n"
        )

    policy, client = _policy(handler)
    async with client:
        assert await policy.allows(f"{BASE}/c", {"events.example.test"})
        assert not await policy.allows(f"{BASE}/b", {"events.example.test"})
        assert policy.crawl_delay(f"{BASE}/c") == 1.5
    assert calls == ["/robots.txt"]


@pytest.mark.asyncio
async def test_meetup_city_search_source_is_blocked_by_robots():
    definition = next(s for s in get_sources_registry().sources if s.name == "meetup_bengaluru")
    definition = definition.model_copy(update={"rate_limit_seconds": 0})
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=MEETUP_ROBOTS)

    policy, client = _policy(handler)
    async with client:
        source = PublicPageEventSource(definition, client, policy, _safety())
        with pytest.raises(SourceFetchError, match="robots policy disallows this URL"):
            await source.fetch()
    assert requested == ["https://www.meetup.com/robots.txt"]


@pytest.mark.asyncio
async def test_robots_redirect_outside_the_domain_fails_closed_and_is_cached():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "events.example.test":
            return httpx.Response(301, headers={"location": "https://other.example.test/robots.txt"})
        return httpx.Response(200, text="User-agent: *\nAllow: /\n")

    policy, client = _policy(handler)
    async with client:
        assert not await policy.allows(f"{BASE}/a", {"events.example.test"})
        assert not await policy.allows(f"{BASE}/b", {"events.example.test"})
    assert requested == [f"{BASE}/robots.txt"]


@pytest.mark.asyncio
async def test_in_domain_robots_redirect_applies_the_final_files_rules():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "events.example.test":
            return httpx.Response(301, headers={"location": "https://cdn.example.test/robots.txt"})
        return httpx.Response(200, text="User-agent: *\nDisallow: /x\n")

    policy, client = _policy(handler)
    domains = {"events.example.test", "cdn.example.test"}
    async with client:
        assert not await policy.allows(f"{BASE}/x", domains)
        assert await policy.allows(f"{BASE}/y", domains)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "allowed"),
    [(404, True), (410, False), (401, False), (403, False), (429, False), (500, False), (503, False)],
)
async def test_robots_status_handling_and_caching(status, allowed):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(status)

    policy, client = _policy(handler)
    async with client:
        assert await policy.allows(f"{BASE}/a", {"events.example.test"}) is allowed
        assert await policy.allows(f"{BASE}/b", {"events.example.test"}) is allowed
    assert calls == ["/robots.txt"]


def test_crawl_delay_does_not_end_a_group_so_a_following_agent_shares_its_rules():
    # Crawl-delay is not a rule line: BadBot joins the "*" group, which therefore
    # disallows everything for EventFinder too, with the group's delay of 10.
    rules = _rules("User-agent: *\nCrawl-delay: 10\nUser-agent: BadBot\nDisallow: /\n")
    assert not rules.allows(BASE + "/anything")
    assert rules.crawl_delay == 10.0


def test_empty_query_marker_is_kept_when_matching():
    rules = _rules("User-agent: *\nDisallow: /a?\n")
    assert not rules.allows(BASE + "/a?")
    assert not rules.allows(BASE + "/a?#frag")
    assert rules.allows(BASE + "/a")
