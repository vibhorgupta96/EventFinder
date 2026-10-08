"""RFC 9309 robots.txt parsing and matching (pure, no I/O)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_HEX_DIGITS = frozenset(b"0123456789abcdefABCDEF")
_PRODUCT_TOKEN = re.compile(r"[A-Za-z_-]+")
_CRAWL_DELAY = re.compile(r"\d+(?:\.\d+)?|\.\d+")


def _canonical(value: str, *, pattern: bool) -> str:
    """Normalize percent-encoding identically for rule patterns and request
    targets (RFC 9309 section 2.2.2): non-ASCII, control, and space octets are
    UTF-8 percent-encoded, %XX of an unreserved character is decoded, other
    %XX is kept with uppercase hex, and a stray "%" becomes "%25". A literal
    "*" or "$" in a request target is encoded, so only a %2A/%24 pattern
    matches it (section 2.2.3); in patterns they stay operators."""

    raw = value.encode("utf-8", "surrogatepass")
    out: list[str] = []
    index = 0
    while index < len(raw):
        octet = raw[index]
        if (
            octet == 0x25
            and index + 2 < len(raw)
            and raw[index + 1] in _HEX_DIGITS
            and raw[index + 2] in _HEX_DIGITS
        ):
            decoded = int(raw[index + 1 : index + 3], 16)
            out.append(chr(decoded) if decoded in _UNRESERVED else f"%{decoded:02X}")
            index += 3
            continue
        if octet == 0x25 or octet <= 0x20 or octet >= 0x7F or (not pattern and octet in b"*$"):
            out.append(f"%{octet:02X}")
        else:
            out.append(chr(octet))
        index += 1
    return "".join(out)


@dataclass(frozen=True, slots=True)
class _Rule:
    allow: bool
    pattern: str
    segments: tuple[str, ...]
    anchored: bool

    @classmethod
    def compile(cls, value: str, *, allow: bool) -> _Rule:
        pattern = _canonical(value, pattern=True)
        anchored = pattern.endswith("$")
        body = (pattern[:-1] if anchored else pattern).replace("$", "%24")
        return cls(allow, pattern, tuple(body.split("*")), anchored)

    def matches(self, target: str) -> bool:
        """Linear-time wildcard match: no regex, so untrusted patterns
        cannot cause catastrophic backtracking."""

        first, *rest = self.segments
        if not target.startswith(first):
            return False
        if not rest:
            return not self.anchored or len(target) == len(first)
        position = len(first)
        for segment in rest[:-1]:
            found = target.find(segment, position)
            if found < 0:
                return False
            position = found + len(segment)
        last = rest[-1]
        if self.anchored:
            return len(target) - len(last) >= position and target.endswith(last)
        return target.find(last, position) >= 0


@dataclass(slots=True)
class _Group:
    agents: set[str] = field(default_factory=set)
    rules: list[_Rule] = field(default_factory=list)
    # Crawl-delay is not an RFC 9309 rule and must not end a group; it is
    # attributed only to the agents named above it within that group.
    delays: list[tuple[frozenset[str], float]] = field(default_factory=list)


def _agent_name(value: str) -> str:
    if value.startswith("*") and (len(value) == 1 or value[1].isspace()):
        return "*"
    match = _PRODUCT_TOKEN.match(value)
    return match.group().casefold() if match else ""


@dataclass(frozen=True, slots=True)
class RobotsRules:
    """The merged rules and crawl-delay that apply to one product token."""

    rules: tuple[_Rule, ...] = ()
    crawl_delay: float = 0.0

    @classmethod
    def allow_all(cls) -> RobotsRules:
        return cls()

    @classmethod
    def parse(cls, text: str, product_token: str) -> RobotsRules:
        token = product_token.casefold()
        groups: list[_Group] = []
        current: _Group | None = None
        collecting_agents = False
        for line in text.removeprefix("\ufeff").splitlines():
            key, separator, value = line.split("#", 1)[0].partition(":")
            if not separator:
                continue
            key, value = key.strip().casefold(), value.strip()
            if key == "user-agent":
                if current is None or not collecting_agents:
                    current = _Group()
                    groups.append(current)
                    collecting_agents = True
                current.agents.add(_agent_name(value))
            elif key in {"allow", "disallow"}:
                if current is None:
                    continue
                collecting_agents = False
                if value:
                    current.rules.append(_Rule.compile(value, allow=key == "allow"))
            elif key == "crawl-delay":
                if current is not None and _CRAWL_DELAY.fullmatch(value):
                    current.delays.append((frozenset(current.agents), float(value)))
            # Sitemap, Host, and unknown records never end or start a group.
        selector = token if any(token in group.agents for group in groups) else "*"
        selected = [group for group in groups if selector in group.agents]
        return cls(
            tuple(rule for group in selected for rule in group.rules),
            max(
                (delay for group in selected for agents, delay in group.delays if selector in agents),
                default=0.0,
            ),
        )

    def allows(self, url: str) -> bool:
        parsed = urlsplit(url)
        if parsed.path == "/robots.txt":
            return True
        # urlsplit drops an empty query, but a trailing "?" still takes part in matching.
        has_query = bool(parsed.query) or url.split("#", 1)[0].endswith("?")
        target = _canonical(
            (parsed.path or "/") + (f"?{parsed.query}" if has_query else ""), pattern=False
        )
        best: tuple[int, bool] | None = None
        for rule in self.rules:
            if rule.matches(target):
                # Longest pattern wins; on equal length True (allow) sorts higher.
                candidate = (len(rule.pattern), rule.allow)
                if best is None or candidate > best:
                    best = candidate
        return best is None or best[1]
