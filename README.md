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
- Rejects explicitly paid, student-only, employee-only, private, product/founder/pitch/sales/career/generic-networking events. Unknown price remains eligible and is shown as `Price not stated`.
- Candidates with missing dates or ambiguous technical fit go to `Needs review`; an AI outage never turns ambiguity into an accepted event.

## Configuration and public interfaces

- `config/config.yaml` controls schedule, horizon, score threshold, and AI provider order.
- `config/sources.yaml` lists public, unauthenticated source adapters and their per-source cadence/rate limit.
- `config/organizers.yaml` controls trust and global-online eligibility.
- `.env` is ignored and is EventFinder-specific; do not copy values from another project.

The read-only interfaces are:

- `GET /` — dashboard and client-side filters
- `GET /api/events` — filtered event JSON (`text`, `topic`, `event_type`, `organizer`, `format`, `registration_state`, `source`, `start_after`, `start_before`, `status`)
- `GET /api/sources` — last source-run health
- `GET /healthz` — database, scheduler, and priority-source freshness

No write HTTP routes or Telegram commands are exposed.

## Source behavior

The built-in adapters use public HTML/JSON-LD/OpenGraph metadata for Luma, Meetup public listings, Hasgeek, Devfolio, Unstop, official engineering surfaces, and search-indexed Eventbrite pages. They do not use privileged APIs: Eventbrite's public event-search API is retired, Meetup API access is restricted, and Luma's API has plan requirements. Source failures are recorded individually, so a 403, CAPTCHA, 429, or markup change does not stop other sources. A 429 pauses the source rather than retrying it aggressively.

## Verification

```sh
make lint
make test
make smoke
git diff --check
```

Tests are offline and fixture-driven. The smoke command uses a temporary SQLite file and never calls a source, starts launchd, or sends Telegram.

The smoke-live Make target is an opt-in, bounded read-only check of at most two
configured public sources. It uses a separate temporary SQLite database, starts no
scheduler or Telegram client, and records robots, access, rate-limit, timeout, and
zero-result outcomes as source observations.
