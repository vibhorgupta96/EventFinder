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
- Rejects explicitly paid, student-only, employee-only, private, product/founder/pitch/sales/career/generic-networking events. Mixed wording such as a free expo with a paid pass/workshop is treated as paid; unknown price remains eligible and is shown as `Price not stated`.
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

The 19 active curated sources cover Bengaluru communities (including GDG, FOSS United, Global AI, CNCF, and Atlassian), established discovery platforms, and official engineering calendars from Google, Databricks, AWS, Microsoft, NVIDIA, CNCF, and GitHub. They use public HTML, JSON feeds, JSON-LD, and OpenGraph metadata; no privileged APIs are used. Eventbrite's public event-search API is retired, Meetup API access is restricted, and Luma's API has plan requirements.

Open Source India, Salesforce Developer Events, Docker Events, and Red Hat Summit Connect remain vetted but disabled source definitions: their current public pages do not expose a stable, bounded event-detail contract. They are deliberately excluded from active coverage until that changes, rather than treating arbitrary provider links or page prose as event facts.

Listing-to-detail hydration is opt-in per source, uses only configured selectors/path prefixes, de-duplicates links, and has a small per-source cap. Every listing and detail request goes through the same URL/DNS safety, configured redirect boundary, robots, rate-limit, access-denial, and CAPTCHA checks. Registration links are never fetched; they are shown only when they pass URL safety and the source's separate registration-domain allowlist. A source failure is recorded individually, so a 403, CAPTCHA, 429, or markup change does not stop other sources; a 429 pauses the source rather than retrying it aggressively.

Known-event refreshes retain the originating source's timezone, date convention,
domain boundaries, and verified organizer attribution. They update only the
known event. Incomplete dates remain unknown rather than borrowing a missing
day from the current date.

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
