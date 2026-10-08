"""Respectful public-source adapters with URL safety, robots, and factual extraction."""

from __future__ import annotations

import asyncio
import json
import re
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta, timezone
from math import isfinite
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from bs4 import BeautifulSoup, Tag
from dateutil import parser as date_parser
from ddgs import DDGS
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from eventfinder.config import SourceDefinition, get_settings
from eventfinder.domain import (
    MEETUP_NO_FEE_TEXT,
    EventCandidate,
    EventFormat,
    EventType,
    FetchResult,
    RegistrationState,
    SourceEvidence,
    free_admission_statement,
    has_explicit_paid_price,
    mentions_payment_terms,
    normalize_price,
)
from eventfinder.robots import RobotsRules
from eventfinder.urls import UnsafeURL, URLSafety, event_identity_url, validate_url_syntax

USER_AGENT = "EventFinder/0.1 (+local read-only technical event discovery)"
# robots.txt groups match this RFC 9309 product token, not the full user agent.
ROBOTS_PRODUCT_TOKEN = USER_AGENT.split("/", 1)[0]
ROBOTS_TTL = timedelta(hours=6)
# A failed/unreachable robots.txt still fails closed (disallow), but caching
# that failure for a shorter, distinct TTL stops every subsequent page fetch
# from re-hammering an already-struggling or misconfigured origin.
NEGATIVE_ROBOTS_TTL = timedelta(minutes=15)
MAX_REDIRECTS = 5
# Bound the terminal (non-redirect) response body read from the network so a
# malicious or misbehaving origin cannot exhaust memory via an oversized or
# decompression-bomb response. Applied to the decoded byte stream as it is
# read, which also caps inflated (gzip/deflate/br) payload sizes.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ROBOTS_BYTES = 512 * 1024
NVIDIA_WEBINAR_PORTAL_URL = "https://www.nvidia.com/en-us/about-nvidia/webinar-portal/"
PLATFORM_CARD_SELECTORS = {
    "luma": "[data-event], [class*='event-card'], a[href*='/event/']",
    "meetup": "[data-event-id], [data-testid*='event'], [class*='event-card']",
    "hasgeek": "article, [class*='event-card'], [data-event]",
    "devfolio": "[data-hackathon], [class*='hackathon-card'], [class*='event-card']",
    "unstop": "[data-opportunity-id], [class*='opportunity-card'], [class*='event-card']",
    "eventbrite": "[data-event-id], [class*='event-card'], article",
    "official": "[data-event], [class*='event-card'], article",
}


class SourceFetchError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class RequestLimiter:
    """Injectable per-origin cadence limiter for every source page request."""

    def __init__(self, sleeper=asyncio.sleep, clock=time.monotonic):
        self.sleeper = sleeper
        self.clock = clock
        self._next_allowed: dict[str, float] = {}

    async def wait(self, url: str, seconds: float) -> None:
        if seconds <= 0:
            return
        origin = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
        now = self.clock()
        delay = max(0.0, self._next_allowed.get(origin, now) - now)
        if delay:
            await self.sleeper(delay)
        self._next_allowed[origin] = self.clock() + seconds


def _allowed_destination(url: str, allowed_domains: set[str] | None) -> bool:
    if not allowed_domains:
        return False
    hostname = (urlsplit(url).hostname or "").casefold()
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in allowed_domains)


def _closed_modal(node: Tag) -> bool:
    classes = set(node.get("class") or [])
    if "modal" not in classes or classes & {"show", "in"} or node.get("aria-modal") == "true":
        return False
    # Script may reveal a modal through its inline style.
    display = re.search(r"display\s*:\s*([a-z-]+)", node.get("style") or "", re.I)
    return display is None or display.group(1).casefold() == "none"


def _dormant_form_widget(widget: Tag) -> bool:
    """A contact-form CAPTCHA inside a closed modal is not a page challenge."""
    form = widget.find_parent("form")
    return form is not None and any(_closed_modal(p) for p in form.parents if getattr(p, "name", None))


def _is_interstitial(text: str) -> bool:
    soup = BeautifulSoup(text, "html.parser")
    # Shared bundles and feature flags commonly mention CAPTCHA even on
    # normal event calendars. Only inspect content a visitor can see.
    for node in soup.select("script, style, template, [hidden], [aria-hidden='true']"):
        node.decompose()
    for node in list(soup.select("[style]")):
        if node.attrs is not None and re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", node.get("style", ""), re.I):
            node.decompose()
    visible_text = soup.get_text(" ", strip=True)
    if re.search(
        r"\b(?:verify (?:that )?you are human|access denied|checking your browser"
        r"|(?:complete|solve|enter)\s+(?:(?:the|a)\s+)?captcha"
        r"|captcha\s+(?:challenge|verification|required))\b",
        visible_text,
        re.I,
    ):
        return True
    if any(re.fullmatch(r"\s*(?:re)?captcha\s*", node.get_text(" ", strip=True), re.I) for node in soup.select("title, h1, h2")):
        return True
    # A rendered challenge widget may have no text until its iframe loads.
    widgets = soup.select(".g-recaptcha, .h-captcha, iframe[src*='/recaptcha/'], iframe[src*='hcaptcha.com']")
    return any(not _dormant_form_widget(widget) for widget in widgets)


class EventSource(ABC):
    def __init__(
        self,
        definition: SourceDefinition,
        client: httpx.AsyncClient,
        robots_policy: RobotsPolicy | None = None,
        safety: URLSafety | None = None,
        limiter: RequestLimiter | None = None,
    ):
        self.definition = definition
        self.client = client
        self.safety = safety or URLSafety()
        self.robots_policy = robots_policy or RobotsPolicy(client, self.safety)
        self.limiter = limiter or RequestLimiter()
        self.allowed_domains = {domain.casefold() for domain in definition.allowed_domains}
        if definition.url and (hostname := urlsplit(definition.url).hostname):
            self.allowed_domains.add(hostname.casefold())
        # The fetch boundary can include a redirect transport host. Do not let
        # that broaden displayable registrations: public-page defaults are the
        # configured source origin, while search sources have no source URL and
        # therefore use their required, explicitly configured result boundary.
        source_owned_domains = (
            {urlsplit(definition.url).hostname.casefold()}
            if definition.url and urlsplit(definition.url).hostname
            else {domain.casefold() for domain in definition.allowed_domains}
        )
        self.allowed_registration_domains = source_owned_domains | {
            domain.casefold() for domain in definition.allowed_registration_domains
        }

    @abstractmethod
    async def fetch(self) -> FetchResult:
        raise NotImplementedError

    async def validated_candidates(self, candidates: list[EventCandidate]) -> list[EventCandidate]:
        """Keep only candidates whose displayed destinations are public and robots-allowed."""
        safe: list[EventCandidate] = []
        for candidate in candidates:
            try:
                candidate.canonical_url = await self.safety.validate(candidate.canonical_url)
                # A source listing may link elsewhere for registration, but its
                # event page must stay on the configured public source boundary.
                # Registration URLs are display-only and handled separately.
                if not _allowed_destination(candidate.canonical_url, self.allowed_domains):
                    continue
                if not await self.robots_policy.allows(
                    candidate.canonical_url, self.allowed_domains
                ):
                    continue
                if candidate.registration_url:
                    try:
                        normalized_registration_url = validate_url_syntax(candidate.registration_url)
                        if not _allowed_destination(
                            normalized_registration_url, self.allowed_registration_domains
                        ):
                            candidate.registration_url = None
                        else:
                            candidate.registration_url = await self.safety.validate(
                                normalized_registration_url
                            )
                    except UnsafeURL:
                        # Keep the factual event, but never expose a destination that
                        # failed the public URL safety boundary. Registration may be
                        # a configured third-party provider, but not an arbitrary link.
                        candidate.registration_url = None
            except UnsafeURL:
                continue
            safe.append(candidate)
        return safe


class RobotsPolicy:
    """Caches RFC 9309 robots rules by origin and checks every requested path."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        safety: URLSafety | None = None,
        ttl: timedelta = ROBOTS_TTL,
        negative_ttl: timedelta = NEGATIVE_ROBOTS_TTL,
    ):
        self.client = client
        self.safety = safety or URLSafety()
        self.ttl = ttl
        self.negative_ttl = negative_ttl
        # (rules or None on failure, cache expiry, discovered crawl-delay seconds)
        self._cache: dict[str, tuple[RobotsRules | None, datetime, float]] = {}

    async def allows(
        self,
        url: str,
        allowed_domains: set[str] | None = None,
        limiter: RequestLimiter | None = None,
        rate_limit_seconds: float = 0,
    ) -> bool:
        safe_url = await self.safety.validate(url)
        if allowed_domains is not None and not _allowed_destination(safe_url, allowed_domains):
            return False
        parsed = urlsplit(safe_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        cached = self._cache.get(origin)
        if cached is None or cached[1] <= datetime.now(UTC):
            rules, crawl_delay = await self._load(
                origin, allowed_domains, limiter, rate_limit_seconds
            )
            ttl = self.ttl if rules is not None else self.negative_ttl
            self._cache[origin] = (rules, datetime.now(UTC) + ttl, crawl_delay)
        rules = self._cache[origin][0]
        if rules is None:
            return False
        return rules.allows(safe_url)

    def crawl_delay(self, url: str) -> float:
        """Return the origin's declared Crawl-delay, or 0 if unknown/unset."""

        parsed = urlsplit(url)
        cached = self._cache.get(f"{parsed.scheme}://{parsed.netloc}")
        return cached[2] if cached else 0.0

    async def _load(
        self,
        origin: str,
        allowed_domains: set[str] | None,
        limiter: RequestLimiter | None,
        rate_limit_seconds: float,
    ) -> tuple[RobotsRules | None, float]:
        try:
            # The robots.txt fetch itself must observe the same per-origin
            # cadence as every other request; it is never a free first hit.
            if limiter:
                await limiter.wait(f"{origin}/robots.txt", rate_limit_seconds)
            response = await _request_redirect_checked(
                self.client,
                f"{origin}/robots.txt",
                self.safety,
                allowed_domains=allowed_domains,
                max_bytes=MAX_ROBOTS_BYTES,
            )
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 404:
                return None, 0.0
            return RobotsRules.allow_all(), 0.0
        except (httpx.HTTPError, SourceFetchError, UnsafeURL):
            return None, 0.0
        rules = RobotsRules.parse(response.text, ROBOTS_PRODUCT_TOKEN)
        return rules, rules.crawl_delay


class _CappedResponse:
    """Lightweight terminal-response facade: exactly the attributes downstream
    parsing needs, decoupled from the transport response's streamed/consumed
    lifecycle once the size-capped body has been read."""

    __slots__ = ("status_code", "headers", "url", "text")

    def __init__(self, status_code: int, headers: httpx.Headers, url: httpx.URL, text: str) -> None:
        self.status_code = status_code
        self.headers = headers
        self.url = url
        self.text = text


async def _read_capped_text(response: httpx.Response, max_bytes: int) -> str:
    """Stream a terminal response body up to ``max_bytes`` decoded bytes,
    guarding against decompression bombs and unbounded downloads."""

    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            await response.aclose()
            raise SourceFetchError("response exceeded max size")
        chunks.append(chunk)
    data = b"".join(chunks)
    encoding = response.charset_encoding
    if encoding:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            pass
    return data.decode("utf-8", errors="replace")


@retry(
    retry=retry_if_exception_type(httpx.TransportError),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=5),
    stop=stop_after_attempt(3),
    reraise=True,
)
async def _request_redirect_checked(
    client: httpx.AsyncClient,
    url: str,
    safety: URLSafety,
    robots_policy: RobotsPolicy | None = None,
    allowed_domains: set[str] | None = None,
    limiter: RequestLimiter | None = None,
    rate_limit_seconds: float = 0,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> _CappedResponse:
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        current = await safety.validate(current)
        if allowed_domains is not None and not _allowed_destination(current, allowed_domains):
            raise SourceFetchError("destination is outside this source's allowed domains")
        if robots_policy and not await robots_policy.allows(
            current, allowed_domains, limiter, rate_limit_seconds
        ):
            raise SourceFetchError("robots policy disallows this URL")
        if limiter:
            # A declared Crawl-delay is a floor, never a ceiling: it can only
            # slow this source down further than its configured cadence.
            effective_rate_limit = (
                max(rate_limit_seconds, robots_policy.crawl_delay(current))
                if robots_policy
                else rate_limit_seconds
            )
            await limiter.wait(current, effective_rate_limit)
        request = client.build_request("GET", current, headers={"User-Agent": USER_AGENT})
        response = await client.send(request, stream=True, follow_redirects=False)
        try:
            if response.status_code == 429:
                raise SourceFetchError("rate limited; source paused", status_code=429)
            if response.status_code in {401, 403}:
                raise SourceFetchError("source denied public access", status_code=response.status_code)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise SourceFetchError("redirect missing location", status_code=response.status_code)
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            text = await _read_capped_text(response, max_bytes)
        finally:
            await response.aclose()
        return _CappedResponse(response.status_code, response.headers, response.url, text)
    raise SourceFetchError("too many redirects")


def _listing_urls(definition: SourceDefinition) -> list[str]:
    assert definition.url
    urls = [definition.url]
    if definition.max_pages > 1 and definition.pagination_param:
        parsed = urlsplit(definition.url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        for page in range(2, definition.max_pages + 1):
            urls.append(
                urlunsplit(
                    (
                        parsed.scheme,
                        parsed.netloc,
                        parsed.path,
                        urlencode({**query, definition.pagination_param: str(page)}),
                        "",
                    )
                )
            )
    return urls


def _matches_detail_prefix(url: str, prefix: str) -> bool:
    parsed = urlsplit(url)
    if prefix.startswith("/"):
        return parsed.path.startswith(prefix)
    return url.startswith(prefix)


def _configured_detail_urls(
    html: str, listing_url: str, definition: SourceDefinition
) -> list[str]:
    """Return only explicitly configured, deduplicated detail links.

    Link selection is intentionally declarative and never follows inferred
    pagination or every anchor on a listing. When both selectors and prefixes
    are configured, a link has to satisfy both rules.
    """

    if not definition.max_detail_pages:
        return []
    soup = BeautifulSoup(html, "html.parser")
    selected_hrefs: set[str] = set()
    if definition.detail_link_selectors:
        try:
            selected_nodes = [
                node
                for selector in definition.detail_link_selectors
                for node in soup.select(selector)
            ]
        except (ValueError, SyntaxError) as error:
            raise SourceFetchError(f"invalid configured detail selector: {error}") from error
        for node in selected_nodes:
            anchor = node if node.name == "a" and node.get("href") else node.select_one("a[href]")
            if anchor and (href := anchor.get("href")):
                selected_hrefs.add(href)

    detail_urls: list[str] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = anchor.get("href")
        if not href:
            continue
        if definition.detail_link_selectors and href not in selected_hrefs:
            continue
        resolved = urlsplit(urljoin(listing_url, href))
        destination = urlunsplit(
            (resolved.scheme, resolved.netloc, resolved.path, resolved.query, "")
        )
        if definition.detail_link_prefixes and not any(
            _matches_detail_prefix(destination, prefix)
            for prefix in definition.detail_link_prefixes
        ):
            continue
        if destination not in seen:
            detail_urls.append(destination)
            seen.add(destination)
    return detail_urls[: definition.max_detail_pages]


class PublicPageEventSource(EventSource):
    async def fetch(self) -> FetchResult:
        observed_at = datetime.now(UTC)
        candidates: list[EventCandidate] = []
        evidence: list[SourceEvidence] = []
        detail_urls: list[str] = []
        seen_detail_urls: set[str] = set()
        for listing_url in _listing_urls(self.definition):
            response = await _request_redirect_checked(
                self.client,
                listing_url,
                self.safety,
                self.robots_policy,
                self.allowed_domains,
                self.limiter,
                self.definition.rate_limit_seconds,
            )
            if _is_interstitial(response.text):
                raise SourceFetchError("source returned CAPTCHA or interstitial")
            parsed = parse_event_page(
                response.text,
                str(response.url),
                self.definition.name,
                observed_at,
                self.definition.platform,
                self.definition.source_timezone,
                self.definition.date_dayfirst,
            )
            if self.definition.max_detail_pages:
                # Listing metadata describes the group/calendar, not an event.
                parsed = [c for c in parsed if c.evidence.facts.get("parser") != "opengraph"]
            candidates.extend(await self.validated_candidates(parsed))
            for detail_url in _configured_detail_urls(
                response.text, str(response.url), self.definition
            ):
                if detail_url not in seen_detail_urls:
                    detail_urls.append(detail_url)
                    seen_detail_urls.add(detail_url)
            evidence.append(
                SourceEvidence(
                    source_name=self.definition.name,
                    source_url=str(response.url),
                    observed_at=observed_at,
                    facts={
                        "parser": f"{self.definition.platform or 'generic'}:json+html",
                        "page_kind": "listing",
                        "candidate_count": len(parsed),
                    },
                )
            )
        for detail_url in detail_urls[: self.definition.max_detail_pages]:
            try:
                response = await _request_redirect_checked(
                    self.client,
                    detail_url,
                    self.safety,
                    self.robots_policy,
                    self.allowed_domains,
                    self.limiter,
                    self.definition.rate_limit_seconds,
                )
            except (httpx.HTTPError, SourceFetchError, UnsafeURL) as error:
                evidence.append(
                    SourceEvidence(
                        source_name=self.definition.name,
                        source_url=detail_url,
                        observed_at=observed_at,
                        facts={
                            "parser": f"{self.definition.platform or 'generic'}:json+html",
                            "page_kind": "detail",
                            "rejected": str(error),
                        },
                    )
                )
                continue
            if _is_interstitial(response.text):
                evidence.append(
                    SourceEvidence(
                        source_name=self.definition.name,
                        source_url=str(response.url),
                        observed_at=observed_at,
                        facts={
                            "parser": f"{self.definition.platform or 'generic'}:json+html",
                            "page_kind": "detail",
                            "rejected": "captcha or interstitial",
                        },
                    )
                )
                continue
            parsed = parse_event_page(
                response.text,
                str(response.url),
                self.definition.name,
                observed_at,
                self.definition.platform,
                self.definition.source_timezone,
                self.definition.date_dayfirst,
            )
            candidates.extend(await self.validated_candidates(parsed))
            evidence.append(
                SourceEvidence(
                    source_name=self.definition.name,
                    source_url=str(response.url),
                    observed_at=observed_at,
                    facts={
                        "parser": f"{self.definition.platform or 'generic'}:json+html",
                        "page_kind": "detail",
                        "candidate_count": len(parsed),
                    },
                )
            )
        return FetchResult(candidates=_dedupe_candidates(candidates), source_evidence=evidence)


class SearchEventSource(EventSource):
    """Hydrates each safe destination; a search snippet never becomes an event by itself."""

    async def fetch(self) -> FetchResult:
        if not self.definition.query:
            return FetchResult()
        ddgs_error: Exception | None = None
        try:
            results = await asyncio.to_thread(
                lambda: list(DDGS().text(self.definition.query, max_results=12))
            )
        except Exception as error:  # ddgs has no stable typed exception surface
            ddgs_error = error
            results = []
        if not results:
            results = await self._fallback_results(self.definition.query, ddgs_error)
        observed_at = datetime.now(UTC)
        candidates: list[EventCandidate] = []
        evidence: list[SourceEvidence] = []
        seen: set[str] = set()
        for result in results:
            destination = result.get("href") or result.get("url")
            if not destination:
                continue
            try:
                response = await _request_redirect_checked(
                    self.client,
                    destination,
                    self.safety,
                    self.robots_policy,
                    self.allowed_domains,
                    self.limiter,
                    self.definition.rate_limit_seconds,
                )
            except (httpx.HTTPError, SourceFetchError, UnsafeURL) as error:
                evidence.append(
                    SourceEvidence(
                        source_name=self.definition.name,
                        source_url=destination,
                        observed_at=observed_at,
                        facts={"parser": "search_hydration", "rejected": str(error)},
                    )
                )
                continue
            if _is_interstitial(response.text):
                evidence.append(
                    SourceEvidence(
                        source_name=self.definition.name,
                        source_url=str(response.url),
                        observed_at=observed_at,
                        facts={"parser": "search_hydration", "rejected": "captcha or interstitial"},
                    )
                )
                continue
            parsed = parse_event_page(
                response.text,
                str(response.url),
                self.definition.name,
                observed_at,
                self.definition.platform,
                self.definition.source_timezone,
                self.definition.date_dayfirst,
            )
            for candidate in await self.validated_candidates(parsed):
                if candidate.canonical_url not in seen:
                    candidates.append(candidate)
                    seen.add(candidate.canonical_url)
            evidence.append(
                SourceEvidence(
                    source_name=self.definition.name,
                    source_url=str(response.url),
                    observed_at=observed_at,
                    facts={"parser": "search_hydration", "candidate_count": len(parsed)},
                )
            )
        return FetchResult(candidates=candidates, source_evidence=evidence)

    async def _fallback_results(
        self, query: str, ddgs_error: Exception | None = None
    ) -> list[dict[str, str]]:
        settings = get_settings()
        providers = (
            ("serper", settings.serper_api_key, self._search_serper),
            ("brave", settings.brave_search_api_key, self._search_brave),
            ("tavily", settings.tavily_api_key, self._search_tavily),
            ("exa", settings.exa_api_key, self._search_exa),
        )
        errors = [f"ddgs: {ddgs_error}"] if ddgs_error is not None else []
        search_succeeded = ddgs_error is None
        for provider, key, search in providers:
            if not key:
                continue
            try:
                results = await search(query, key)
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
                errors.append(f"{provider}: {error}")
                continue
            search_succeeded = True
            if results:
                return results
        if errors and not search_succeeded:
            raise SourceFetchError("search unavailable: " + "; ".join(errors))
        return []

    async def _search_serper(self, query: str, key: str) -> list[dict[str, str]]:
        response = await self.client.post("https://google.serper.dev/search", headers={"X-API-KEY": key}, json={"q": query, "num": 12})
        response.raise_for_status()
        return [{"href": x["link"]} for x in response.json().get("organic", []) if x.get("link")]

    async def _search_brave(self, query: str, key: str) -> list[dict[str, str]]:
        response = await self.client.get("https://api.search.brave.com/res/v1/web/search", headers={"X-Subscription-Token": key}, params={"q": query, "count": 12})
        response.raise_for_status()
        return [{"href": x["url"]} for x in response.json().get("web", {}).get("results", []) if x.get("url")]

    async def _search_tavily(self, query: str, key: str) -> list[dict[str, str]]:
        response = await self.client.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {key}"},
            json={"query": query, "max_results": 12},
        )
        response.raise_for_status()
        return [{"href": x["url"]} for x in response.json().get("results", []) if x.get("url")]

    async def _search_exa(self, query: str, key: str) -> list[dict[str, str]]:
        response = await self.client.post("https://api.exa.ai/search", headers={"x-api-key": key}, json={"query": query, "numResults": 12})
        response.raise_for_status()
        return [{"href": x["url"]} for x in response.json().get("results", []) if x.get("url")]


def make_source(
    definition: SourceDefinition,
    client: httpx.AsyncClient,
    robots_policy: RobotsPolicy | None = None,
    safety: URLSafety | None = None,
    limiter: RequestLimiter | None = None,
) -> EventSource:
    if definition.adapter == "public_page":
        return PublicPageEventSource(definition, client, robots_policy, safety, limiter)
    if definition.adapter == "search":
        return SearchEventSource(definition, client, robots_policy, safety, limiter)
    raise ValueError(f"unsupported source adapter: {definition.adapter}")


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value] if value is not None else []


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return " ".join(value.split()) or None
    if isinstance(value, dict):
        return _text(value.get("name") or value.get("title") or value.get("@id"))
    return None


def _parse_time(value: Any, tz: str = "Asia/Kolkata") -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = date_parser.isoparse(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz))
    return parsed.astimezone(UTC)


def _json_nodes(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        nodes = [value]
        for key in ("@graph", "events", "data", "results", "items", "edges", "itemListElement"):
            child = value.get(key)
            if isinstance(child, (dict, list)):
                nodes.extend(_json_nodes(child))
        if "ListItem" in str(value.get("@type", "")) and isinstance(value.get("item"), (dict, list)):
            nodes.extend(_json_nodes(value["item"]))
        return nodes
    if isinstance(value, list):
        return [node for item in value for node in _json_nodes(item)]
    return []


def _location(value: Any) -> tuple[str | None, str | None, str | None]:
    if isinstance(value, str):
        city = "Bengaluru" if re.search(r"\bbangal(?:ore|uru)\b", value, re.I) else None
        return value, city, None
    if not isinstance(value, dict):
        return None, None, None
    address = value.get("address") if isinstance(value.get("address"), dict) else value
    return (_text(value.get("name")) or _text(address.get("streetAddress")), _text(address.get("addressLocality") or address.get("city")), _text(address.get("addressCountry") or address.get("country")))


def _event_type(title: str, description: str) -> EventType:
    text = f"{title} {description}".casefold()
    pairs = (("buildathon", EventType.BUILDATHON), ("hackathon", EventType.HACKATHON), ("competition", EventType.COMPETITION), ("challenge", EventType.COMPETITION), ("workshop", EventType.WORKSHOP), ("hands-on", EventType.WORKSHOP), ("conference", EventType.CONFERENCE), ("summit", EventType.CONFERENCE), ("meetup", EventType.MEETUP))
    for needle, event_type in pairs:
        if needle in text:
            return event_type
    return EventType.TALK if any(x in text for x in ("talk", "speaker", "session", "webinar")) else EventType.UNKNOWN


def _registration_state(text: str) -> RegistrationState:
    lowered = text.casefold()
    if lowered.strip() in {"open", "registration_open", "registration open"}:
        return RegistrationState.OPEN
    if re.search(r"\bcancel(?:led|ed|lation)?\b", lowered):
        return RegistrationState.CANCELLED
    if "postpon" in lowered:
        return RegistrationState.POSTPONED
    if "sold out" in lowered:
        return RegistrationState.SOLD_OUT
    if "almost full" in lowered:
        return RegistrationState.OPEN
    if "waitlist" in lowered:
        return RegistrationState.WAITLIST
    if re.search(r"\b(?:registration(?:s)? (?:are|have|is) )?closed\b", lowered) or "ended" in lowered:
        return RegistrationState.CLOSED
    return (
        RegistrationState.OPEN
        if re.search(
            r"\b(register|registration(?:s)? (?:are )?(?:now )?open(?:ed)?|rsvp|apply now|tickets? available)\b",
            lowered,
        )
        else RegistrationState.UNKNOWN
    )


def _offer_values(
    offers: Any, tz: str = "Asia/Kolkata"
) -> tuple[str | None, str | None, str | None, datetime | None, bool]:
    prices: list[str] = []
    registration_url: str | None = None
    availability: str | None = None
    valid_from: datetime | None = None
    has_paid_offer = False
    for offer in _as_list(offers):
        if isinstance(offer, (str, int, float)) and not isinstance(offer, bool):
            if price := normalize_price(offer):
                prices.append(price)
                has_paid_offer = has_paid_offer or has_explicit_paid_price(price)
            continue
        if not isinstance(offer, dict):
            continue
        price = normalize_price(_first_present(offer, "price", "amount", "fee"))
        currency = _text(offer.get("priceCurrency") or offer.get("currency"))
        if price:
            offer_price = f"{currency or ''} {price}".strip()
            prices.append(offer_price)
            has_paid_offer = has_paid_offer or has_explicit_paid_price(price) or has_explicit_paid_price(offer_price)
        registration_url = registration_url or _text(offer.get("url") or offer.get("checkoutUrl"))
        availability = availability or _text(offer.get("availability"))
        valid_from = valid_from or _parse_time(offer.get("validFrom"), tz)
    # A multi-tier offer may include free admission and a paid pass/workshop.
    # Retain every explicitly displayed price so policy can reject any paid tier.
    return "; ".join(dict.fromkeys(prices)) or None, registration_url, availability, valid_from, has_paid_offer


def _schema_registration_state(event_status: Any, availability: str | None) -> RegistrationState:
    status = _text(event_status) or ""
    availability_text = availability or ""
    combined = f"{status} {availability_text}".casefold()
    if "eventcancelled" in combined or "cancel" in combined:
        return RegistrationState.CANCELLED
    if "postpon" in combined:
        return RegistrationState.POSTPONED
    if "soldout" in combined or "outofstock" in combined:
        return RegistrationState.SOLD_OUT
    if "discontinued" in combined or "closed" in combined or "ended" in combined:
        return RegistrationState.CLOSED
    if "instock" in combined or "preorder" in combined:
        return RegistrationState.OPEN
    return RegistrationState.UNKNOWN


def _is_paid(price: str | None) -> bool:
    return has_explicit_paid_price(price)


def _format(value: Any, venue: str | None, is_online: Any = None) -> EventFormat:
    # An event's explicit attendance mode outranks venue and online flags.
    # In particular, a venue or unrelated page prose must not turn an
    # OfflineEventAttendanceMode event into an online one.
    mode = (_text(value) or "").casefold()
    in_person = "offline" in mode or "in person" in mode or "in-person" in mode
    online = "online" in mode or "virtual" in mode
    if "hybrid" in mode or "mixed" in mode or (in_person and online):
        return EventFormat.HYBRID
    if in_person:
        return EventFormat.IN_PERSON
    if online:
        return EventFormat.ONLINE
    if str(is_online).casefold() == "true":
        return EventFormat.ONLINE
    venue_text = (venue or "").casefold()
    if "online" in venue_text or "virtual" in venue_text:
        return EventFormat.ONLINE
    return EventFormat.IN_PERSON if venue else EventFormat.UNKNOWN


def _local_datetime(node: dict[str, Any], prefix: str) -> str | None:
    """Combine documented Meetup-style local date/time fields when present."""

    date = _text(node.get(f"{prefix}_date") or node.get(f"{prefix}Date"))
    time = _text(node.get(f"{prefix}_time") or node.get(f"{prefix}Time"))
    return f"{date}T{time}" if date and time else date


def _candidate_from_mapping(
    node: dict[str, Any],
    page_url: str,
    source_name: str,
    observed_at: datetime,
    parser_name: str,
    tz: str = "Asia/Kolkata",
) -> EventCandidate | None:
    title = _text(node.get("name") or node.get("title") or node.get("eventName"))
    start = _parse_time(
        node.get("startDate")
        or node.get("start_time")
        or node.get("startTime")
        or node.get("starts_at")
        or node.get("start_at")
        or node.get("startAt")
        or node.get("start")
        or _local_datetime(node, "local"),
        tz,
    )
    kind = node.get("@type") or node.get("type") or ""
    if not title or not ("event" in str(kind).casefold() or start or node.get("registrationDeadline")):
        return None
    description = _text(node.get("description") or node.get("summary") or node.get("about") or node.get("blurb")) or ""
    event_url = _text(node.get("url") or node.get("eventUrl") or node.get("event_url") or node.get("permalink") or node.get("link")) or page_url
    venue, city, country = _location(
        node.get("location") or node.get("venue") or node.get("geo_address_info") or {"city": node.get("city")}
    )
    price, offer_url, offer_availability, offer_valid_from, has_paid_offer = _offer_values(
        node.get("offers") or node.get("ticket") or node.get("pricing") or node.get("tickets"), tz
    )
    price = price or normalize_price(_first_present(node, "price", "fee", "price_text"))
    if not price and node.get("isAccessibleForFree") is True:
        price = "Free admission"
    admission_statement = free_admission_statement(description)
    price = price or admission_statement
    registration_url = _text(node.get("registrationUrl") or node.get("registration_url") or node.get("registrationLink") or node.get("registerUrl") or node.get("applyUrl") or node.get("actionUrl")) or offer_url or event_url
    eligibility = _text(node.get("eligibility") or node.get("eligibility_text") or node.get("eligibilityText") or node.get("audience") or node.get("requirements"))
    speakers = [name for item in _as_list(node.get("performer") or node.get("speakers") or node.get("speaker") or node.get("presenters")) if (name := _text(item))]
    explicit_registration_status = _text(
        node.get("registrationStatus") or node.get("registration_state") or node.get("eventStatus")
    )
    if explicit_registration_status is None and node.get("isRegistrationOpen") is True:
        explicit_registration_status = "open"
    state_text = " ".join(
        filter(
            None,
            [
                title,
                description,
                explicit_registration_status,
                price,
            ],
        )
    )
    evidence = SourceEvidence(source_name=source_name, source_url=page_url, observed_at=observed_at, raw_id=_text(node.get("@id") or node.get("id")), facts={"parser": parser_name, "event": node, "source_timezone": tz})
    if admission_statement:
        evidence.facts["admission_statement"] = admission_statement
    schema_state = _schema_registration_state(node.get("eventStatus"), offer_availability)
    parsed_state = _registration_state(explicit_registration_status) if explicit_registration_status else _registration_state(state_text)
    return EventCandidate(
        title=title, canonical_url=urljoin(page_url, event_url), source_url=page_url, source_name=source_name,
        organizer=_text(node.get("organizer") or node.get("host") or node.get("organization") or node.get("organizerName")), description=description,
        starts_at=start, ends_at=_parse_time(node.get("endDate") or node.get("end_time") or node.get("endTime") or node.get("ends_at") or node.get("end_at") or node.get("endAt") or _local_datetime(node, "local_end"), tz),
        venue=venue, city=city, country=country, format=_format(node.get("eventAttendanceMode") or node.get("format") or node.get("event_format") or node.get("mode"), venue, node.get("isOnline") or node.get("is_online")),
        event_type=_event_type(title, description), registration_state=(
            schema_state if schema_state != RegistrationState.UNKNOWN else parsed_state
        ), registration_url=urljoin(page_url, registration_url),
        registration_deadline=_parse_time(node.get("registrationDeadline") or node.get("registration_deadline") or node.get("registration_closes_at") or node.get("applicationDeadline") or node.get("deadline"), tz),
        registration_opened_at=_parse_time(node.get("registrationOpenedAt") or node.get("registration_opened_at") or node.get("registration_opened") or node.get("registrationOpenDate") or node.get("registrationDate"), tz) or offer_valid_from,
        price_text=price, is_explicitly_paid=has_paid_offer or _is_paid(price), eligibility_text=eligibility, speakers=speakers,
        topics=[topic for topic in _as_list(node.get("topics") or node.get("tags") or node.get("categories")) if isinstance(topic, str)], evidence=evidence,
    )


def _html_card_candidates(
    soup: BeautifulSoup,
    page_url: str,
    source_name: str,
    observed_at: datetime,
    platform: str | None,
    tz: str = "Asia/Kolkata",
) -> list[EventCandidate]:
    selector = PLATFORM_CARD_SELECTORS.get(platform or "", "[data-event], [class*='event-card'], article")
    candidates: list[EventCandidate] = []
    for card in soup.select(selector):
        anchor = card.select_one("a[href]")
        title_node = card.select_one("[data-event-title], [class*='title'], h1, h2, h3, h4") or anchor
        title = title_node.get_text(" ", strip=True) if title_node else ""
        if not title or not anchor:
            continue
        attrs = card.attrs
        mapping: dict[str, Any] = {
            "@type": "Event", "name": title, "url": anchor.get("href"), "description": card.get_text(" ", strip=True)[:2000],
            "startDate": attrs.get("data-start") or attrs.get("data-start-date") or attrs.get("data-start-time"),
            "endDate": attrs.get("data-end") or attrs.get("data-end-date"), "registrationDeadline": attrs.get("data-registration-deadline") or attrs.get("data-deadline"),
            "registrationUrl": attrs.get("data-registration-url") or attrs.get("data-register-url"), "price": attrs.get("data-price"),
            "registrationStatus": attrs.get("data-registration-state") or attrs.get("data-registration-status"),
            "registrationOpenedAt": attrs.get("data-registration-opened-at") or attrs.get("data-registration-open-date"),
            "eligibility": attrs.get("data-eligibility"), "location": {"name": attrs.get("data-venue"), "city": attrs.get("data-city")}, "format": attrs.get("data-format"),
        }
        candidate = _candidate_from_mapping(mapping, page_url, source_name, observed_at, f"{platform or 'generic'}:html", tz)
        if candidate:
            candidates.append(candidate)
    return candidates


def _label_value(soup: BeautifulSoup, labels: tuple[str, ...]) -> str | None:
    """Read documented label/value adjacency without guessing from page prose."""

    normalized_labels = {label.casefold() for label in labels}
    for text_node in soup.find_all(string=True):
        label = " ".join(text_node.strip().split()).casefold()
        if label.rstrip(":") not in normalized_labels:
            continue
        parent = text_node.parent
        sibling = parent.find_next_sibling()
        if sibling:
            value = sibling.get_text(" ", strip=True)
            if value:
                return value
        if parent.name in {"dt", "th"}:
            sibling = parent.find_next_sibling(["dd", "td"])
            if sibling:
                value = sibling.get_text(" ", strip=True)
                if value:
                    return value
        container = parent.parent
        if container:
            value = container.get_text(" ", strip=True)
            value = re.sub(rf"^{re.escape(text_node.strip())}\s*:?[\s-]*", "", value, flags=re.I)
            if value and value != text_node.strip():
                return value
    return None


_MONTH_NAME_PATTERN = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
    re.I,
)
_YEAR_LAST_NUMERIC_DATE_PATTERN = re.compile(r"\b\d{1,2}[-/]\d{1,2}[-/]20\d{2}\b")
_NAMED_MONTH_DAY_PATTERN = re.compile(
    rf"(?:\b\d{{1,2}}(?:st|nd|rd|th)?[\s,/-]+{_MONTH_NAME_PATTERN.pattern}"
    rf"|{_MONTH_NAME_PATTERN.pattern}[\s,/-]+\d{{1,2}}(?:st|nd|rd|th)?(?!\d))",
    re.I,
)
# An explicit year-first numeric date (e.g. "2026-10-11") is already
# unambiguous: the two-digit fields that follow are month-then-day. The
# source's dayfirst preference must only disambiguate genuinely ambiguous,
# year-last numeric dates (e.g. "03/04/2026"); dateutil's dayfirst otherwise
# also reorders an already-unambiguous year-first date's month/day pair.
_YEAR_FIRST_NUMERIC_DATE_PATTERN = re.compile(r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b")


def _label_time(value: str | None, tz: str = "Asia/Kolkata", dayfirst: bool = True) -> datetime | None:
    """Parse an explicitly labelled full date only; never supply a missing month/day/year."""

    if not value or not re.search(r"\b20\d{2}\b", value):
        return None
    if not (
        _NAMED_MONTH_DAY_PATTERN.search(value)
        or _YEAR_FIRST_NUMERIC_DATE_PATTERN.search(value)
        or _YEAR_LAST_NUMERIC_DATE_PATTERN.search(value)
    ):
        return None
    effective_dayfirst = False if _YEAR_FIRST_NUMERIC_DATE_PATTERN.search(value) else dayfirst
    try:
        parsed = date_parser.parse(value, fuzzy=True, dayfirst=effective_dayfirst)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz))
    return parsed.astimezone(UTC)


def _semantic_candidate(
    soup: BeautifulSoup,
    page_url: str,
    source_name: str,
    observed_at: datetime,
    platform: str | None,
    tz: str = "Asia/Kolkata",
    dayfirst: bool = True,
) -> EventCandidate | None:
    """Platform-detail fallback for public pages whose facts are semantic labels."""

    title_node = soup.select_one("h1, main h2")
    title = title_node.get_text(" ", strip=True) if title_node else ""
    starts_text = _label_value(
        soup,
        ("starts", "runs from", "happening", "date and time", "date & time", "date"),
    )
    starts_at = _label_time(starts_text, tz, dayfirst)
    if starts_at is None:
        time_node = soup.select_one("time[datetime]")
        starts_at = _parse_time(time_node.get("datetime") if time_node else None, tz)
    if not title or not starts_at:
        return None
    ends_at = _label_time(_label_value(soup, ("ends", "ends at", "runs until")), tz, dayfirst)
    venue = _label_value(soup, ("venue", "location", "address", "where"))
    city = "Bengaluru" if venue and re.search(r"\b(?:bengaluru|bangalore|blr)\b", venue, re.I) else None
    organizer = _label_value(soup, ("presented by", "hosted by", "host", "organizer"))
    registration_state_text = _label_value(
        soup,
        ("registration", "registration status", "applications", "application status", "status"),
    )
    page_text = soup.get_text(" ", strip=True)
    registration_state = _registration_state(registration_state_text or page_text)
    deadline = _label_time(
        _label_value(soup, ("registration deadline", "apply by", "sales end", "applications close")),
        tz,
        dayfirst,
    )
    eligibility = _label_value(soup, ("eligibility", "who can participate", "who can apply"))
    event_container = title_node.find_parent(["main", "article"]) if title_node else None
    # A sidebar can be inside <main>, and generic detail pages may have no
    # <main> at all. Search a copy without navigation/other-event cards so their
    # prices cannot become facts about the current heading's event.
    price_scope = BeautifulSoup(str(event_container or soup), "html.parser")
    for unrelated in price_scope.select(
        "aside, nav, footer, .event-card, [data-event-id], [class*='related'], [class*='recommend']"
    ):
        if unrelated.name is None:
            continue
        heading = unrelated.select_one("h1")
        if heading and heading.get_text(" ", strip=True) == title:
            continue
        unrelated.decompose()
    price = _label_value(price_scope, ("fee", "cost", "price", "entry fee"))
    format_text = _label_value(soup, ("format", "event format", "mode", "how to attend"))
    registration_anchor = soup.find(
        "a",
        href=True,
        string=re.compile(r"register|registration|rsvp|apply|ticket", re.I),
    )
    registration_url = urljoin(page_url, registration_anchor["href"]) if registration_anchor else page_url
    speakers_value = _label_value(soup, ("speakers", "speaker"))
    speakers = [speakers_value] if speakers_value else []
    evidence = SourceEvidence(
        source_name=source_name,
        source_url=page_url,
        observed_at=observed_at,
        facts={"parser": f"{platform or 'generic'}:semantic_labels", "admission_price": price},
    )
    return EventCandidate(
        title=title,
        canonical_url=page_url,
        source_url=page_url,
        source_name=source_name,
        organizer=organizer,
        description="",
        starts_at=starts_at,
        ends_at=ends_at,
        venue=venue,
        city=city,
        format=_format(format_text, venue),
        event_type=_event_type(title, page_text),
        registration_state=registration_state,
        registration_url=registration_url,
        registration_deadline=deadline,
        registration_opened_at=_label_time(
            _label_value(
                soup,
                ("registration opened", "registration open date", "applications opened"),
            ),
            tz,
            dayfirst,
        ),
        price_text=price,
        is_explicitly_paid=_is_paid(price),
        eligibility_text=eligibility,
        speakers=speakers,
        evidence=evidence,
    )


_MEETUP_MODES = {"PHYSICAL": "offline", "ONLINE": "online", "HYBRID": "mixed"}
_MEETUP_RSVP_STATES = {
    "JOIN_OPEN": RegistrationState.OPEN,
    "CLOSED": RegistrationState.CLOSED,
    "WAITLIST": RegistrationState.WAITLIST,
}


def _first_path_segment(url: str) -> str:
    return urlsplit(url).path.strip("/").split("/")[0].casefold()


def _meetup_apollo_candidates(
    soup: BeautifulSoup,
    page_url: str,
    source_name: str,
    observed_at: datetime,
    tz: str = "Asia/Kolkata",
) -> list[EventCandidate] | None:
    """Read a Meetup page's own public Apollo state, scoped to the page's group."""
    script = soup.select_one("script#__NEXT_DATA__")
    if script is None:
        return None
    try:
        state = json.loads(script.get_text())["props"]["pageProps"]["__APOLLO_STATE__"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(state, dict):
        return None
    root = state.get("ROOT_QUERY")
    root = root if isinstance(root, dict) else {}
    slug = _first_path_segment(page_url)

    def in_scope_group(ref: Any) -> dict[str, Any] | None:
        group = state.get(ref.get("__ref", "")) if isinstance(ref, dict) else None
        if not isinstance(group, dict) or str(group.get("urlname") or "").casefold() != slug:
            return None
        return None if group.get("isPrivate") is True else group

    refs: list[Any] = []
    for key, value in root.items():
        if key.startswith("groupByUrlname:"):
            group = in_scope_group(value)
            for group_key, connection in (group or {}).items():
                if group_key.startswith("events(") and '"afterDateTime"' in group_key and isinstance(connection, dict):
                    refs.extend(
                        edge["node"].get("__ref")
                        for edge in connection.get("edges") or []
                        if isinstance(edge, dict) and isinstance(edge.get("node"), dict)
                    )
        elif key.startswith("event(") and isinstance(value, dict):
            refs.append(value.get("__ref"))

    candidates: list[EventCandidate] = []
    for ref in dict.fromkeys(ref for ref in refs if isinstance(ref, str)):
        node = state.get(ref)
        if not isinstance(node, dict):
            continue
        event_url = node.get("eventUrl")
        if (
            not isinstance(event_url, str)
            or _first_path_segment(event_url) != slug
            or (urlsplit(event_url).hostname or "").casefold() not in {"meetup.com", "www.meetup.com"}
        ):
            continue
        group = in_scope_group(node.get("group"))
        if group is None or node.get("status") not in {"ACTIVE", "CANCELLED"}:
            continue
        online = node.get("eventType") == "ONLINE" or node.get("isOnline") is True
        mapping: dict[str, Any] = {
            "@type": "Event", "name": node.get("title"), "url": event_url,
            "description": node.get("description"), "startDate": node.get("dateTime"),
            "endDate": node.get("endTime"), "organizer": group.get("name"),
            "eventAttendanceMode": _MEETUP_MODES.get(node.get("eventType")),
            "isOnline": node.get("isOnline"),
        }
        venue = state.get(node["venue"].get("__ref", "")) if isinstance(node.get("venue"), dict) else None
        if isinstance(venue, dict) and not online:
            mapping["location"] = {
                "name": venue.get("name"),
                "address": {"streetAddress": venue.get("address"), "addressLocality": venue.get("city")},
            }
        fee_present = "feeSettings" in node
        fee = node.get("feeSettings")
        unresolved_ref = False
        if isinstance(fee, dict) and isinstance(fee.get("__ref"), str):
            fee = state.get(fee["__ref"])
            unresolved_ref = not isinstance(fee, dict)
        paid = False
        sanitized_fee = None
        suppressed = False
        # Only a literal null fee on a confirmed non-network event is free evidence.
        if node.get("feeSettings", False) is None and node.get("isNetworkEvent") is False:
            if mentions_payment_terms(f"{mapping['name'] or ''} {mapping['description'] or ''}"):
                suppressed = True
            else:
                mapping["price"] = MEETUP_NO_FEE_TEXT
        elif isinstance(fee, dict):
            amount = fee.get("amount")
            sanitized_fee = {key: fee.get(key) for key in ("amount", "currency", "accepts")}
            if isinstance(amount, (int, float)) and not isinstance(amount, bool) and isfinite(amount) and amount > 0:
                mapping["price"] = f"{fee.get('currency') or ''} {normalize_price(amount)}".strip()
                paid = True
        candidate = _candidate_from_mapping(mapping, page_url, source_name, observed_at, "meetup:apollo", tz)
        if candidate is None:
            continue
        candidate.is_explicitly_paid = candidate.is_explicitly_paid or paid
        rsvp_state = node.get("rsvpState")
        rsvp_settings = node.get("rsvpSettings")
        if node.get("status") == "CANCELLED":
            candidate.registration_state = RegistrationState.CANCELLED
        elif isinstance(rsvp_settings, dict) and rsvp_settings.get("rsvpsClosed") is True and rsvp_state != "WAITLIST":
            candidate.registration_state = RegistrationState.CLOSED
        else:
            candidate.registration_state = _MEETUP_RSVP_STATES.get(rsvp_state, RegistrationState.UNKNOWN)
        candidate.evidence.raw_id = str(node["id"]) if node.get("id") else None
        # Only sanitized facts are kept; the raw node carries member data.
        if unresolved_ref:
            candidate.evidence.facts["meetup_fee_settings"] = {"unresolved_ref": True}
        elif fee_present:
            candidate.evidence.facts["meetup_fee_settings"] = (
                sanitized_fee if fee is None or isinstance(fee, dict) else {"unparsed_type": type(fee).__name__}
            )
        if mapping.get("price") == MEETUP_NO_FEE_TEXT:
            candidate.evidence.facts["admission_evidence"] = "meetup_fee_settings_null"
        elif suppressed:
            candidate.evidence.facts["admission_evidence"] = "meetup_fee_settings_null_suppressed_by_payment_terms"
        if isinstance(node.get("status"), str):
            candidate.evidence.facts["meetup_status"] = node["status"]
        if isinstance(rsvp_state, str):
            candidate.evidence.facts["meetup_rsvp_state"] = rsvp_state
        candidates.append(candidate)
    return candidates


OCG_APPROVAL_TEXT = "Attendee approval required"
_OCG_ATTENDANCE_ATTRS = {
    "data-canceled": "canceled",
    "data-event-timezone": "event_timezone",
    "data-registration-window-open": "registration_window_open",
    "data-registration-window-message": "registration_window_message",
    "data-is-simple-rsvp": "is_simple_rsvp",
    "data-paid-capable": "paid_capable",
    "data-ticket-is-free-only": "ticket_is_free_only",
    "data-has-sold-out-ticket-types": "has_sold_out_ticket_types",
    "data-attendee-approval-required": "attendee_approval_required",
    "data-starts": "starts",
    "data-waitlist-enabled": "waitlist_enabled",
}
_OCG_FULL_DATE = re.compile(r"[A-Z][a-z]+ \d{1,2}, 20\d{2}")
_OCG_TIME_RANGE = re.compile(r"(\d{1,2}:\d{2} [AP]M) - (\d{1,2}:\d{2} [AP]M) [A-Z]{2,5}")
_OCG_GROUP_PATH = re.compile(r"/[a-z0-9-]+/group/[a-z0-9]+")
_OCG_EVENT_PATH = re.compile(r"/[a-z0-9-]+/group/[a-z0-9]+/event/[a-z0-9]+/?")


def _ocg_text(node: Tag | None) -> str | None:
    return (" ".join(node.get_text(" ", strip=True).split()) or None) if node is not None else None


def _ocg_end_time(panel: Tag | None, starts_at: datetime, event_timezone: str | None) -> datetime | None:
    """Displayed end time, used only when the same panel's start matches data-starts."""
    if panel is None or not event_timezone:
        return None
    try:
        zone = ZoneInfo(event_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    texts = [_ocg_text(child) or "" for child in panel.find_all("div", recursive=False)]
    day = next((text for text in texts if _OCG_FULL_DATE.fullmatch(text)), None)
    hours = next((match for text in texts if (match := _OCG_TIME_RANGE.fullmatch(text))), None)
    if day is None or hours is None:
        return None
    try:
        start = date_parser.parse(f"{day} {hours.group(1)}").replace(tzinfo=zone)
        end = date_parser.parse(f"{day} {hours.group(2)}").replace(tzinfo=zone)
    except (TypeError, ValueError, OverflowError):
        return None
    if start.astimezone(UTC) != starts_at or end <= start:
        return None
    return end.astimezone(UTC)


def _ocg_event_candidates(
    soup: BeautifulSoup,
    page_url: str,
    source_name: str,
    observed_at: datetime,
    tz: str = "Asia/Kolkata",
) -> list[EventCandidate] | None:
    """Read an Open Community Groups event page's own public attendance attributes."""
    container = soup.select_one("div#attendance-container-main[data-attendance-container]")
    if container is None:
        if _OCG_EVENT_PATH.fullmatch(urlsplit(page_url).path):
            return []  # Event URL whose widget markup drifted: fail closed, no page-prose fallback.
        return None  # Not an OCG event page (e.g. group listing): existing parsers apply.
    attendance = {
        key: " ".join(str(container.get(attr)).split())
        for attr, key in _OCG_ATTENDANCE_ATTRS.items()
        if container.get(attr) is not None
    }
    header = container.find_parent("div", class_="grid")
    heading = header.select_one("h1") if header is not None else None
    title = _ocg_text(heading)
    starts_at = _parse_time(attendance.get("starts"), tz)
    if heading is None or not title or starts_at is None:
        return []  # The page owns its widget; never fall back to page-wide prose.
    organizer = None
    group_anchor = heading.find_previous_sibling("a", href=True)
    if group_anchor is not None:
        try:
            group_path = urlsplit(urljoin(page_url, group_anchor["href"])).path.rstrip("/")
        except ValueError:
            group_path = ""
        if _OCG_GROUP_PATH.fullmatch(group_path) and urlsplit(page_url).path.startswith(f"{group_path}/event/"):
            organizer = _ocg_text(group_anchor)
    venue = None
    for label in soup.find_all("div", string=re.compile(r"^\s*Location\s*$")):
        card = label.parent.parent if label.parent is not None else None
        if card is None or label.find_parent(attrs={"role": "dialog"}) is not None:
            continue
        venue = next((text for pill in card.select("div.absolute.bottom-2.left-2")
                      if pill.find_parent(attrs={"role": "dialog"}) is None and (text := _ocg_text(pill))), None)
        break
    city = "Bengaluru" if venue and re.search(r"\b(?:bengaluru|bangalore|blr)\b", venue, re.I) else None
    badge = _ocg_text(header.select_one("span.custom-badge")) if header is not None else None
    mode = badge if badge and badge.casefold() in {"in-person", "virtual", "hybrid"} else None
    tickets = [
        {
            "price_minor": " ".join(str(ticket.get("data-ticket-price-minor", "")).split()),
            "sold_out": " ".join(str(ticket.get("data-ticket-sold-out", "")).split()),
            "purchasable": " ".join(str(ticket.get("data-ticket-purchasable", "")).split()),
        }
        for ticket in container.select("input[data-attendance-role='ticket-type-option']")
    ]
    badges = [_ocg_text(node) or "" for node in container.select("[data-attendance-role='ticket-type-price-badge']")]
    paid = any(ticket["price_minor"].isdecimal() and int(ticket["price_minor"]) > 0 for ticket in tickets)
    free = (
        not paid
        and attendance.get("ticket_is_free_only") == "true"
        and attendance.get("paid_capable") != "true"
        and bool(tickets)
        and all(ticket["price_minor"] == "0" for ticket in tickets)
        and bool(badges)
        and all(text.casefold() == "free" for text in badges)
    )
    price = "Free" if free else (next((text for text in badges if text and text.casefold() != "free"), None) if paid else None)
    if attendance.get("canceled") == "true":
        state = RegistrationState.CANCELLED
    elif tickets and all(ticket["sold_out"] == "true" for ticket in tickets):
        state = RegistrationState.WAITLIST if attendance.get("waitlist_enabled") == "true" else RegistrationState.SOLD_OUT
    elif attendance.get("registration_window_open") == "true":
        state = RegistrationState.OPEN
    elif attendance.get("registration_window_open") == "false" and attendance.get(
        "registration_window_message", ""
    ).casefold().startswith("registration closed"):
        state = RegistrationState.CLOSED
    else:
        state = RegistrationState.UNKNOWN
    about = next((node for node in soup.find_all("div", string=re.compile(r"^\s*About this event\s*$"))), None)
    description = (_ocg_text(about.find_next_sibling("div")) if about is not None else None) or ""
    description = description[:2000]
    page_view = soup.select_one("[data-page-view][data-entity-type='event'][data-entity-id]")
    facts: dict[str, Any] = {
        "parser": "ocg:attendance",
        "source_timezone": tz,
        "ocg_attendance": attendance,
        "ocg_tickets": tickets,
        "admission_price": price,
    }
    return [EventCandidate(
        title=title, canonical_url=page_url, source_url=page_url, source_name=source_name,
        organizer=organizer, description=description, starts_at=starts_at,
        ends_at=_ocg_end_time(soup.select_one("[data-registration-window-date-panel]"), starts_at,
                              attendance.get("event_timezone")),
        venue=venue, city=city, format=_format(mode, venue), event_type=_event_type(title, description),
        registration_state=state, registration_url=page_url, price_text=price, is_explicitly_paid=paid,
        eligibility_text=OCG_APPROVAL_TEXT if attendance.get("attendee_approval_required") == "true" else None,
        evidence=SourceEvidence(
            source_name=source_name, source_url=page_url, observed_at=observed_at,
            raw_id=page_view.get("data-entity-id") if page_view is not None else None, facts=facts,
        ),
    )]


def parse_event_page(
    html: str,
    page_url: str,
    source_name: str,
    observed_at: datetime | None = None,
    platform: str | None = None,
    tz: str = "Asia/Kolkata",
    dayfirst: bool = True,
) -> list[EventCandidate]:
    """Extract JSON-LD, embedded public JSON, and platform card markup without invented facts."""
    observed_at = observed_at or datetime.now(UTC)
    if platform == "nvidia_webinar":
        return _nvidia_webinar_candidates(html, page_url, source_name, observed_at)
    soup = BeautifulSoup(html, "html.parser")
    if platform == "ocg" and (ocg := _ocg_event_candidates(soup, page_url, source_name, observed_at, tz)) is not None:
        return ocg
    candidates: list[EventCandidate] = []
    for script in soup.select("script[type='application/ld+json'], script[type='application/json'], script#__NEXT_DATA__"):
        try:
            payload = json.loads(script.get_text(strip=True))
        except json.JSONDecodeError:
            continue
        parser_name = "json_ld" if script.get("type") == "application/ld+json" else "embedded_json"
        for node in _json_nodes(payload):
            candidate = _candidate_from_mapping(node, page_url, source_name, observed_at, parser_name, tz)
            if candidate:
                candidates.append(candidate)
    candidates.extend(_html_card_candidates(soup, page_url, source_name, observed_at, platform, tz))
    if semantic := _semantic_candidate(soup, page_url, source_name, observed_at, platform, tz, dayfirst):
        candidates.append(semantic)
    apollo = _meetup_apollo_candidates(soup, page_url, source_name, observed_at, tz) if platform == "meetup" else None
    candidates.extend(apollo or [])
    if candidates:
        return _dedupe_candidates(candidates)
    if apollo is not None:
        return []  # Meetup state present but no in-scope events: no OpenGraph group junk.
    title_tag = soup.find("meta", property="og:title") or soup.title
    title = title_tag.get("content", "").strip() if title_tag and title_tag.name == "meta" else (title_tag.get_text(strip=True) if title_tag else None)
    if not title:
        return []
    description_tag = soup.find("meta", property="og:description")
    description = description_tag.get("content", "").strip() if description_tag else ""
    canonical = soup.find("link", rel="canonical")
    canonical_url = canonical.get("href") if canonical else page_url
    evidence_text = " ".join(filter(None, [title, description, soup.get_text(" ", strip=True)]))[:3000]
    evidence = SourceEvidence(source_name=source_name, source_url=page_url, observed_at=observed_at, facts={"parser": "opengraph", "title": title, "description": description})
    return [EventCandidate(title=title, canonical_url=urljoin(page_url, canonical_url), source_url=page_url, source_name=source_name, description=description, event_type=_event_type(title, description), registration_state=_registration_state(evidence_text), evidence=evidence)]


def _utc_epoch_milliseconds(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _nvidia_displayed_start(value: Any) -> datetime | None:
    """Parse only complete portal dates with a published, explicit fixed zone."""

    if not isinstance(value, str) or not re.search(r"\b20\d{2}\b", value):
        return None
    if not (
        _NAMED_MONTH_DAY_PATTERN.search(value)
        or _YEAR_FIRST_NUMERIC_DATE_PATTERN.search(value)
        or _YEAR_LAST_NUMERIC_DATE_PATTERN.search(value)
    ):
        return None
    zone = re.search(r"\b(CET|PST)\s*$", value, re.I)
    if not zone or not re.search(r"\b\d{1,2}(?::\d{2}|\s*(?:AM|PM)\b)", value, re.I):
        return None
    normalized = value[: zone.start()] + zone.group(1).upper()
    try:
        parsed = date_parser.parse(
            normalized,
            tzinfos={"CET": timezone(timedelta(hours=1)), "PST": timezone(timedelta(hours=-8))},
        )
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _nvidia_webinar_candidates(
    text: str, page_url: str, source_name: str, observed_at: datetime
) -> list[EventCandidate]:
    """Read the public portal's feed and its published numeric HashRouter route."""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []
    candidates = []
    for row in payload["data"]:
        if not isinstance(row, dict) or row.get("type") != "Upcoming":
            continue
        event_id = row.get("eventId")
        if isinstance(event_id, bool) or not isinstance(event_id, (int, str)):
            continue
        if not re.fullmatch(r"[0-9]+", str(event_id)) or not str(event_id).strip("0"):
            continue
        title = _text(row.get("title"))
        if not title:
            continue
        live_start = _utc_epoch_milliseconds(row.get("liveStartTimeInUTC"))
        has_displayed_date = row.get("eventStartDate") is not None and row.get("eventStartDate") != ""
        starts_at = _nvidia_displayed_start(row.get("eventStartDate")) if has_displayed_date else live_start
        ends_at = _utc_epoch_milliseconds(row.get("liveEndTimeInUTC"))
        if starts_at is None or (ends_at is not None and ends_at < starts_at):
            ends_at = None
        description = BeautifulSoup(_text(row.get("eventAbstract")) or "", "html.parser").get_text(" ", strip=True) or None
        canonical_url = f"{NVIDIA_WEBINAR_PORTAL_URL}#/webinar/{event_id}"
        evidence = SourceEvidence(
            source_name=source_name,
            source_url=page_url,
            observed_at=observed_at,
            raw_id=str(event_id),
            facts={
                "parser": "nvidia_webinar_feed",
                "event": row,
                "source_timezone": "UTC",
                "portal_url": NVIDIA_WEBINAR_PORTAL_URL,
                "portal_route": "/webinar/:webinarId",
                "scheduled_start_field": "eventStartDate" if has_displayed_date else "liveStartTimeInUTC",
            },
        )
        if has_displayed_date and starts_at is None:
            evidence.facts["start_time_error"] = "displayed eventStartDate has no complete date with a recognized explicit timezone"
        if has_displayed_date and starts_at is not None and live_start is not None and starts_at != live_start:
            evidence.facts["start_time_disagreement"] = {
                "displayed_start": starts_at.isoformat(),
                "live_start": live_start.isoformat(),
            }
        candidates.append(
            EventCandidate(
                title=title,
                canonical_url=canonical_url,
                source_url=page_url,
                source_name=source_name,
                description=description,
                starts_at=starts_at,
                ends_at=ends_at,
                format=EventFormat.ONLINE if row.get("mediaType") == "Webcast" else EventFormat.UNKNOWN,
                event_type=_event_type(title, description or ""),
                evidence=evidence,
            )
        )
    return _dedupe_candidates(candidates)


_REGISTRATION_STATE_SEVERITY = {
    RegistrationState.UNKNOWN: 0,
    RegistrationState.OPEN: 1,
    RegistrationState.WAITLIST: 2,
    RegistrationState.CLOSED: 3,
    RegistrationState.SOLD_OUT: 4,
    RegistrationState.POSTPONED: 5,
    RegistrationState.CANCELLED: 6,
}


def _combined_text(first: str | None, second: str | None) -> str | None:
    values = [value for value in (first, second) if value]
    return "; ".join(dict.fromkeys(values)) or None


def _earliest(first: datetime | None, second: datetime | None) -> datetime | None:
    if first is None:
        return second
    if second is None:
        return first
    return min(first, second)


def _merged_evidence(first: SourceEvidence, second: SourceEvidence) -> SourceEvidence:
    """Keep detail provenance primary and retain every flattened observation."""

    evidence = second.model_copy(deep=True)
    observations = [
        *_flattened_observations(first),
        *_flattened_observations(second),
    ]
    evidence.facts["merged_observations"] = observations
    first_urls = first.facts.get("merged_source_urls", [first.source_url])
    second_urls = second.facts.get("merged_source_urls", [second.source_url])
    urls = list(dict.fromkeys([*first_urls, *second_urls]))
    if len(urls) > 1:
        evidence.facts["merged_source_urls"] = urls
    return evidence


def _flattened_observations(evidence: SourceEvidence) -> list[dict[str, Any]]:
    """Return auditable evidence payloads without nesting prior merge payloads."""

    merged = evidence.facts.get("merged_observations")
    if isinstance(merged, list):
        return [item.copy() for item in merged if isinstance(item, dict)]
    facts = {
        key: value
        for key, value in evidence.facts.items()
        if key not in {"merged_observations", "merged_source_urls"}
    }
    return [
        {
            "source_url": evidence.source_url,
            "raw_id": evidence.raw_id,
            "observed_at": evidence.observed_at.isoformat(),
            "facts": facts,
        }
    ]


def _explicit_json_ld_format(candidate: EventCandidate) -> EventFormat | None:
    """Keep schema attendance evidence ahead of weaker HTML observations."""

    # Flattened observations are chronological within a merged candidate.
    # A later detail page can correct an earlier listing's schema mode.
    for observation in reversed(_flattened_observations(candidate.evidence)):
        facts = observation.get("facts", {})
        if not isinstance(facts, dict) or facts.get("parser") != "json_ld":
            continue
        event = facts.get("event")
        if not isinstance(event, dict) or not event.get("eventAttendanceMode"):
            continue
        event_format = _format(event["eventAttendanceMode"], None)
        if event_format != EventFormat.UNKNOWN:
            return event_format
    return None


def _explicit_json_ld_time(candidate: EventCandidate, field: str) -> datetime | None:
    """Use event-specific schema dates ahead of page-wide semantic labels."""

    for observation in reversed(_flattened_observations(candidate.evidence)):
        facts = observation.get("facts", {})
        if not isinstance(facts, dict) or facts.get("parser") != "json_ld":
            continue
        event = facts.get("event")
        if not isinstance(event, dict) or not event.get(field):
            continue
        raw_value = event[field]
        # A schema calendar date parses as midnight, but cannot override a
        # semantic label that supplies the event's actual clock time.
        if not isinstance(raw_value, str) or not re.search(r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}", raw_value):
            continue
        parsed = _parse_time(raw_value, facts.get("source_timezone", "Asia/Kolkata"))
        if parsed is not None:
            return parsed
    return None


def _merge_candidates(existing: EventCandidate, candidate: EventCandidate) -> EventCandidate:
    """Merge complementary observations without weakening policy-relevant facts."""

    merged = candidate.model_copy(deep=True)
    merged.title = candidate.title or existing.title
    merged.source_url = candidate.source_url or existing.source_url
    merged.organizer = candidate.organizer or existing.organizer
    merged.description = _combined_text(existing.description, candidate.description)
    merged.starts_at = (
        _explicit_json_ld_time(candidate, "startDate")
        or _explicit_json_ld_time(existing, "startDate")
        or candidate.starts_at
        or existing.starts_at
    )
    merged.ends_at = (
        _explicit_json_ld_time(candidate, "endDate")
        or _explicit_json_ld_time(existing, "endDate")
        or candidate.ends_at
        or existing.ends_at
    )
    merged.venue = candidate.venue or existing.venue
    merged.city = candidate.city or existing.city
    merged.country = candidate.country or existing.country
    merged.format = (
        _explicit_json_ld_format(candidate)
        or _explicit_json_ld_format(existing)
        or (candidate.format if candidate.format != EventFormat.UNKNOWN else existing.format)
    )
    merged.event_type = (
        candidate.event_type if candidate.event_type != EventType.UNKNOWN else existing.event_type
    )
    merged.registration_state = max(
        (existing.registration_state, candidate.registration_state),
        key=lambda state: _REGISTRATION_STATE_SEVERITY[state],
    )
    merged.registration_url = candidate.registration_url or existing.registration_url
    # An earlier deadline/opening must not be silently replaced with a more
    # permissive observation. Detail end/start values still supply missing facts.
    merged.registration_deadline = _earliest(
        existing.registration_deadline, candidate.registration_deadline
    )
    merged.registration_opened_at = _earliest(
        existing.registration_opened_at, candidate.registration_opened_at
    )
    merged.first_observed_open_at = _earliest(
        existing.first_observed_open_at, candidate.first_observed_open_at
    )
    merged.price_text = _combined_text(existing.price_text, candidate.price_text)
    merged.is_explicitly_paid = (
        existing.is_explicitly_paid
        or candidate.is_explicitly_paid
        or has_explicit_paid_price(merged.price_text)
    )
    merged.eligibility_text = _combined_text(existing.eligibility_text, candidate.eligibility_text)
    merged.speakers = list(dict.fromkeys([*existing.speakers, *candidate.speakers]))
    merged.topics = list(dict.fromkeys([*existing.topics, *candidate.topics]))
    merged.evidence = _merged_evidence(existing.evidence, candidate.evidence)
    return merged


def _dedupe_candidates(candidates: list[EventCandidate]) -> list[EventCandidate]:
    deduped: dict[str, EventCandidate] = {}
    for candidate in candidates:
        identity = event_identity_url(candidate.canonical_url)
        existing = deduped.get(identity)
        deduped[identity] = (
            candidate if existing is None else _merge_candidates(existing, candidate)
        )
    return list(deduped.values())
