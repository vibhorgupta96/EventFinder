"""Respectful public-source adapters with URL safety, robots, and factual extraction."""

from __future__ import annotations

import asyncio
import json
import re
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from ddgs import DDGS
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from eventfinder.config import SourceDefinition, get_settings
from eventfinder.domain import (
    EventCandidate,
    EventFormat,
    EventType,
    FetchResult,
    RegistrationState,
    SourceEvidence,
    has_explicit_paid_price,
    normalize_price,
)
from eventfinder.urls import UnsafeURL, URLSafety, validate_url_syntax

USER_AGENT = "EventFinder/0.1 (+local read-only technical event discovery)"
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


def _is_interstitial(text: str) -> bool:
    return bool(re.search(r"captcha|verify you are human|access denied|checking your browser", text, re.I))


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
    """Caches RobotFileParsers by origin and checks every requested path."""

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
        # (parser or None on failure, cache expiry, discovered crawl-delay seconds)
        self._cache: dict[str, tuple[RobotFileParser | None, datetime, float]] = {}

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
            parser, crawl_delay = await self._load(
                origin, allowed_domains, limiter, rate_limit_seconds
            )
            ttl = self.ttl if parser is not None else self.negative_ttl
            self._cache[origin] = (parser, datetime.now(UTC) + ttl, crawl_delay)
        parser = self._cache[origin][0]
        if parser is None:
            return False
        return parser.can_fetch(USER_AGENT, safe_url)

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
    ) -> tuple[RobotFileParser | None, float]:
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
            parser = RobotFileParser()
            parser.parse(["User-agent: *", "Allow: /"])
            return parser, 0.0
        except (httpx.HTTPError, SourceFetchError, UnsafeURL):
            return None, 0.0
        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser, float(parser.crawl_delay(USER_AGENT) or 0.0)


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
        try:
            results = await asyncio.to_thread(
                lambda: list(DDGS().text(self.definition.query, max_results=12))
            )
        except Exception as error:  # ddgs has no stable typed exception surface
            results = await self._fallback_results(self.definition.query, error)
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

    async def _fallback_results(self, query: str, ddgs_error: Exception) -> list[dict[str, str]]:
        settings = get_settings()
        providers = (
            (settings.serper_api_key, self._search_serper),
            (settings.brave_search_api_key, self._search_brave),
            (settings.tavily_api_key, self._search_tavily),
            (settings.exa_api_key, self._search_exa),
        )
        errors = [f"ddgs: {ddgs_error}"]
        for key, search in providers:
            if not key:
                continue
            try:
                return await search(query, key)
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
                errors.append(str(error))
        raise SourceFetchError("search unavailable: " + "; ".join(errors))

    async def _search_serper(self, query: str, key: str) -> list[dict[str, str]]:
        response = await self.client.post("https://google.serper.dev/search", headers={"X-API-KEY": key}, json={"q": query, "num": 12})
        response.raise_for_status()
        return [{"href": x["link"]} for x in response.json().get("organic", []) if x.get("link")]

    async def _search_brave(self, query: str, key: str) -> list[dict[str, str]]:
        response = await self.client.get("https://api.search.brave.com/res/v1/web/search", headers={"X-Subscription-Token": key}, params={"q": query, "count": 12})
        response.raise_for_status()
        return [{"href": x["url"]} for x in response.json().get("web", {}).get("results", []) if x.get("url")]

    async def _search_tavily(self, query: str, key: str) -> list[dict[str, str]]:
        response = await self.client.post("https://api.tavily.com/search", json={"api_key": key, "query": query, "max_results": 12})
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
        for key in ("@graph", "events", "data", "results", "items", "edges"):
            child = value.get(key)
            if isinstance(child, (dict, list)):
                nodes.extend(_json_nodes(child))
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
) -> tuple[str | None, str | None, str | None, datetime | None]:
    prices: list[str] = []
    registration_url: str | None = None
    availability: str | None = None
    valid_from: datetime | None = None
    for offer in _as_list(offers):
        if isinstance(offer, (str, int, float)) and not isinstance(offer, bool):
            if price := normalize_price(offer):
                prices.append(price)
            continue
        if not isinstance(offer, dict):
            continue
        price = normalize_price(_first_present(offer, "price", "amount", "fee"))
        currency = _text(offer.get("priceCurrency") or offer.get("currency"))
        if price:
            prices.append(f"{currency or ''} {price}".strip())
        registration_url = registration_url or _text(offer.get("url") or offer.get("checkoutUrl"))
        availability = availability or _text(offer.get("availability"))
        valid_from = valid_from or _parse_time(offer.get("validFrom"), tz)
    # A multi-tier offer may include free admission and a paid pass/workshop.
    # Retain every explicitly displayed price so policy can reject any paid tier.
    return "; ".join(dict.fromkeys(prices)) or None, registration_url, availability, valid_from


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
    text = " ".join(filter(None, [_text(value) or "", venue or "", str(is_online or "")])).casefold()
    if "hybrid" in text or "mixed" in text:
        return EventFormat.HYBRID
    if "online" in text or "virtual" in text or str(is_online).casefold() == "true":
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
    price, offer_url, offer_availability, offer_valid_from = _offer_values(
        node.get("offers") or node.get("ticket") or node.get("pricing") or node.get("tickets"), tz
    )
    price = price or normalize_price(_first_present(node, "price", "fee", "price_text"))
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
    evidence = SourceEvidence(source_name=source_name, source_url=page_url, observed_at=observed_at, raw_id=_text(node.get("@id") or node.get("id")), facts={"parser": parser_name, "event": node})
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
        price_text=price, is_explicitly_paid=_is_paid(price), eligibility_text=eligibility, speakers=speakers,
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
# A day/month numeric token, e.g. the "10-10" in "2026-10-10" or "05/03" in
# "05/03/2026". This is deliberately loose: it only gates whether the text
# carries a real date beyond a bare year, never the value that gets parsed.
_NUMERIC_DATE_TOKEN_PATTERN = re.compile(r"\d{1,2}[/-]\d{1,2}")
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
    if not (_MONTH_NAME_PATTERN.search(value) or _NUMERIC_DATE_TOKEN_PATTERN.search(value)):
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
    price = _label_value(soup, ("fee", "cost", "price", "entry fee"))
    if not price and re.search(r"\bfree(?: of cost)?\b", page_text, re.I):
        price = "Free"
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
        facts={"parser": f"{platform or 'generic'}:semantic_labels"},
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
        format=_format(format_text, venue, "online" in page_text.casefold()),
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
    soup = BeautifulSoup(html, "html.parser")
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
    if candidates:
        return _dedupe_candidates(candidates)
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


def _merge_candidates(existing: EventCandidate, candidate: EventCandidate) -> EventCandidate:
    """Merge complementary observations without weakening policy-relevant facts."""

    merged = candidate.model_copy(deep=True)
    merged.title = candidate.title or existing.title
    merged.source_url = candidate.source_url or existing.source_url
    merged.organizer = candidate.organizer or existing.organizer
    merged.description = _combined_text(existing.description, candidate.description)
    merged.starts_at = candidate.starts_at or existing.starts_at
    merged.ends_at = candidate.ends_at or existing.ends_at
    merged.venue = candidate.venue or existing.venue
    merged.city = candidate.city or existing.city
    merged.country = candidate.country or existing.country
    merged.format = (
        candidate.format if candidate.format != EventFormat.UNKNOWN else existing.format
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
        existing = deduped.get(candidate.canonical_url)
        deduped[candidate.canonical_url] = (
            candidate if existing is None else _merge_candidates(existing, candidate)
        )
    return list(deduped.values())
