# EventFinder

EventFinder is a local, read-only service that finds free technical talks, workshops, conferences, hackathons, and buildathons. It prioritizes Bengaluru in-person/hybrid events and trusted global online events, then exposes them at `http://127.0.0.1:8766` and optionally sends a silent-if-unchanged Telegram digest at 9:00 AM IST.

It never registers for an event, fills a form, writes to a calendar, imports sibling-project credentials, or bypasses robots rules, CAPTCHAs, authentication, or access controls.

## Quick start

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
cp .env.example .env
make install
make migrate
make smoke
```

Configure `EVENTFINDER_TELEGRAM_BOT_TOKEN` and `EVENTFINDER_TELEGRAM_CHAT_ID` only after creating a separate bot with BotFather. `EVENTFINDER_GEMINI_API_KEY` and `EVENTFINDER_GROQ_API_KEY` are optional and only classify otherwise ambiguous candidates. No missing secret prevents the dashboard from running.

For foreground development, run:

```sh
uv run eventfinder
```

For the persistent Mac-local service (uses its own `com.eventfinder.app` launchd label, `caffeinate -i`, and `~/Library/Logs/EventFinder` logs), run:

```sh
make start
make status
make logs
make stop
```

`make start` needs the isolated `.venv` created by `make install`; it does not start any sibling project. The launchd template is versioned at `launchd/com.eventfinder.app.plist`; the lifecycle wrapper renders the real local paths during installation.

## Policy

- Includes technical/AI/developer talks, workshops, practitioner conferences, hackathons, buildathons, and engineering competitions.
- Allows events up to 60 days ahead; it allows up to 180 days only when registration opened in the last seven days.
- Allows Bengaluru/Bangalore in-person or hybrid events, plus online events from organizers marked as trusted and online-enabled in `config/organizers.yaml`.
- Notifications require source evidence of free admission. Unknown prices stay in `Needs review`; incidental free Wi-Fi, discounts or exam offers do not verify admission. Mixed free/paid admission is rejected. For Meetup events, an explicitly empty public fee setting on a non-network event is recorded as `No Meetup fee` and counts as free evidence; a listed Meetup fee, a price or required charge in the event description, or a previously stored paid status always wins, any payment wording or currency amount in the title or description withholds the fee-setting evidence, and a missing fee field stays unverified.
- Rejects student-only, employee-only, private, product/founder/pitch/sales/career events, social-only mixers and certification/exam/credential promotions. An incidental speaker credential does not exclude a substantive technical talk.
- A past-start series lasting more than 14 days needs an actual upcoming occurrence; short ongoing multi-day events remain allowed. Stored rows and unsent retries are checked against these rules before delivery, preserving sent history.
- Candidates with missing dates or ambiguous technical fit go to `Needs review`; an AI outage never turns ambiguity into an accepted event.

## Configuration and public interfaces

- `config/config.yaml` controls schedule, horizon, score threshold, and AI provider order.
- `config/sources.yaml` lists public, unauthenticated source adapters, their per-source cadence/rate limit, optional bounded listing-to-detail rules, and display-only registration-link domains.
- `config/organizers.yaml` controls trust and global-online eligibility.
- `.env` is ignored and is EventFinder-specific; do not copy values from another project.

The read-only interfaces are:

- `GET /` — dashboard and client-side filters
- `GET /api/events` — filtered event JSON (`text`, `topic`, `event_type`, `organizer`, `format`, `registration_state`, `source`, `start_after`, `start_before`, `status`)
- `GET /api/sources` — last source-run health
- `GET /healthz` — database, scheduler, and priority-source freshness

No write HTTP routes or Telegram commands are exposed.

Dashboard date filters cover whole days in IST, including the selected end day.
The API also accepts timestamps with an explicit timezone for precise bounds.

Priority-source health requires at least half of the enabled priority sources
(rounded up) to have fresh successful runs. Freshness is measured against twice
each source's configured cadence. The health response exposes the coverage
counts and source states so one working source cannot hide a wider outage.

## Source behavior

The 30 active curated sources cover Bengaluru communities (including GDG, FOSS United, Global AI, CNCF, and Atlassian), established discovery platforms, and official engineering calendars from Google, Databricks, AWS, Microsoft, NVIDIA, CNCF, and GitHub. They use public HTML, JSON feeds, JSON-LD, and OpenGraph metadata; no privileged APIs are used. Eventbrite's public event-search API is retired, Meetup API access is restricted, and Luma's API has plan requirements. Luma uses the public luma.com calendar's JSON-LD with a small capped set of detail pages. UiPath, GDG Cloud, Snowflake, MuleSoft and Trailblazer chapter pages hydrate only the chapter's own upcoming event links, and the Atlassian and GDG Bengaluru chapters are limited to their own upcoming events in the same way. The CNCF Cloud Native Bangalore chapter is read from its canonical public Open Community Groups page (ocgroups.dev), because the community.cncf.io vanity URL's robots.txt redirects off-domain and fails closed; only its own upcoming events are hydrated, using each event page's public attendance attributes (cancellation, registration window, ticket prices, attendee approval) rather than page prose. Meetup group pages are read from the page's own public embedded state, scoped to that group, with no API calls. A closed contact-form reCAPTCHA inside a hidden modal is not treated as a challenge; visible challenges still fail the source. Meetup's city search (`/find/?location=`) is disallowed by Meetup's robots.txt, so `meetup_bengaluru` is disabled.

Open Source India, Salesforce Developer Events, Docker Events, and Red Hat Summit Connect remain vetted but disabled source definitions: their current public pages do not expose a stable, bounded event-detail contract. They are deliberately excluded from active coverage until that changes, rather than treating arbitrary provider links or page prose as event facts.

Listing-to-detail hydration is opt-in per source, uses only configured selectors/path prefixes, de-duplicates links, and has a small per-source cap. Every listing and detail request goes through the same URL/DNS safety, configured redirect boundary, robots, rate-limit, access-denial, and CAPTCHA checks. Registration links are never fetched; they are shown only when they pass URL safety and the source's separate registration-domain allowlist. A source failure is recorded individually, so a 403, CAPTCHA, 429, or markup change does not stop other sources; a 429 pauses the source rather than retrying it aggressively. Robots rules follow RFC 9309: every group naming the `EventFinder` product token (otherwise every `*` group) is merged, `*`/`$` wildcards apply, the longest matching rule wins and Allow wins ties, and Crawl-delay can only slow a source. A missing robots.txt (404) allows crawling; other errors, or a robots redirect outside the source's domains, fail closed.

Known-event refreshes retain the originating source's timezone, date convention,
domain boundaries, and verified organizer attribution. They update only the
known event. Incomplete dates remain unknown rather than borrowing a missing
day from the current date.

Each refresh reserves a small share of its 50-event cap for upcoming records
held only because free admission is unverified. These detail requests retain
the original source's safety boundaries and cadence, including after failures,
and reassess every eligibility rule before any notification becomes available.
An explicit statement such as "The event is free of cost" in the event's own
description counts as admission evidence; incidental free amenities and
sidebar prices do not.

NVIDIA webinars use the public JSON feed linked by NVIDIA's portal script and
the portal's published event routes. Feed timestamps and descriptions remain
source evidence; an upcoming listing does not establish a free price or open
registration. Refresh reads the feed and updates only the matching known event.

Both the service and live smoke checks pin public-source connections to validated
public IP addresses. A network that resolves a source to a non-public address
will leave that source unavailable until public DNS resolution is restored.

Digest retries retain their saved message bodies and track which event changes
each chunk contains. If retries are exhausted, a later digest carries only the
unsent chunks; successfully delivered chunks remain complete. Cancellation and
other terminal registration states are stated explicitly in notifications.
These terminal updates require a previous successful notification for the event;
a record held in review cannot produce its first notification after registration closes.

## Verification

```sh
make lint
make test
make smoke
git diff --check
```

Tests are offline and fixture-driven. The smoke command uses a temporary SQLite file and never calls a source, starts launchd, or sends Telegram.

The smoke-live Make target is an opt-in, bounded read-only check of at most two
configured public sources. To target a source (and raise the bound only when needed),
run:

```sh
uv run python -m eventfinder.live_smoke --source foss_united_bengaluru
uv run python -m eventfinder.live_smoke --max-sources 2 --source google_developers --source databricks_events
```

It uses a separate temporary SQLite database, starts no scheduler or Telegram client,
and records robots, access, rate-limit, timeout, and zero-result outcomes as source
observations. A requested name must be enabled and cannot exceed `--max-sources`.
