# EventFinder repository guidance

- This is a standalone local service. Never read, import, copy credentials from, or modify sibling projects.
- Keep public-source collection respectful: no authentication, CAPTCHA bypass, access-control circumvention, or registration automation.
- Preserve factual provenance. AI can classify only ambiguous candidates and must never supply facts absent from source evidence.
- Keep tests offline: use HTTP fixtures and mocked Telegram/AI clients.
- The dashboard and APIs are read-only. Any side-effect belongs only in the explicitly configured Telegram sender.
- Before a release, run `make lint`, `make test`, `make smoke`, and `git diff --check`.

