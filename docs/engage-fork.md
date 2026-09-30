# Engagement tools in this fork

This fork of stickerdaniel/linkedin-mcp-server adds four tools, registered by
`tools/engage.py` next to the upstream ones:

| Tool | Arguments | Returns |
|------|-----------|---------|
| `react_to_post` | `post_url`, `reaction` (like, celebrate, support, love, insightful, funny) | `url`, `status`, `reaction`, `retry_safe` |
| `comment_on_post` | `post_url`, `text` | `url`, `status`, `retry_safe` |
| `get_connection_state` | `linkedin_username` | `url`, `state` |
| `get_post_author` | `post_url` | `url`, `name`, `username` |

- `react_to_post` statuses: `reacted`, `already_reacted`, `post_unavailable`,
  `react_failed`, `outcome_unknown`.
- `comment_on_post` statuses: `commented`, `comments_disabled`,
  `post_unavailable`, `comment_failed`, `outcome_unknown`.
- `get_connection_state` states are `detect_connection_state()`'s, read without
  a click: `pending`, `already_connected`, `connectable`, `follow_only`,
  `incoming_request`, `self_profile`, `unavailable`.
- `get_post_author` gives `username` null for a company post, and both fields
  null when the post could not be read.

`post_url` takes `/feed/update/<urn>/`, `/posts/<slug>`, either as a relative
path, or a bare `urn:li:activity|share|ugcPost:<id>`
(`identifiers.normalize_post_url`).

The transport's own `outcome_unknown` (`retry_safe: false`) still applies to
the two write tools, like every other tool that is not `readOnlyHint`.

## Selectors

`scraping/engage_actions.py` explains each one. They follow the AGENTS.md
rules, so no decision reads a label. They were built from saved fixtures
(`tests/fixtures/engage/`), not from a live page, so they are a claim about
LinkedIn's markup until the live check below confirms it:

- the react trigger is the first `button[aria-pressed]` in `<main>`
- reactions are told apart by `data-test-reactions-icon-type`
- the comment editor is the first visible `[role=textbox][contenteditable]` in
  `<main>`, and its Post button is the one `button[type=submit]` of its form
- the Comment button is the next button after the react trigger in the bar
- the author is the first `/in/` or `/company/` link in `<main>`

## Upstream test baseline

Measured on Windows 11 with Python 3.13, `uv run pytest -o addopts="" -ra`:

| Tree | Passed | Failed | Skipped |
|------|--------|--------|---------|
| upstream `6eb4d1f` | 6237 | 244 | 298 |

The 244 failures are upstream's on this machine: release-notes scripts,
container detection and POSIX-only process tests. None touch the scraping or
tool code.

The full suite was not re-run on this branch, because it starts real Chromium
and used too much memory on the development PC. Instead, every test file
covering changed code was run on its own with the browser and integration
markers deselected. The two failures left there also fail upstream on this
machine: `test_canonical_fixtures_are_portable_deterministic_json` (the
checkout has CRLF line endings; the committed LF bytes pass) and
`test_the_owner_entry_point_says_so_before_it_can_fail`. The DOM tests in
`tests/scraping/test_engage_actions_dom.py` passed, 11 of 11, in their last
run before that restriction.

## Live check

Not run yet. It needs Noor signed in with `--login`, on his own post
`urn:li:share:7510371678175096832`: `react_to_post` with `like`, then
`comment_on_post` with "Test, removing this now.", each checked on LinkedIn
and undone by hand.
