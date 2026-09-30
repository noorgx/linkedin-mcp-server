# Engagement tools in this fork

This fork of stickerdaniel/linkedin-mcp-server adds four tools, registered by
`tools/engage.py` next to the upstream ones:

| Tool | Arguments | Returns |
|------|-----------|---------|
| `react_to_post` | `post_url`, `reaction` (like, celebrate, support, love, insightful, funny) | `url`, `status`, `reaction`, `retry_safe` |
| `comment_on_post` | `post_url`, `text` | `url`, `status`, `retry_safe` |
| `get_connection_state` | `linkedin_username` | `url`, `state` |
| `get_post_author` | `post_url` | `url`, `status`, `name`, `username` |

- `react_to_post` statuses: `reacted`, `already_reacted`, `post_unavailable`,
  `react_failed`, `outcome_unknown`. `reacted` is reported only when the post's
  react button shows the requested reaction type afterwards. A pressed button
  whose type cannot be read never counts: before a click it gives
  `already_reacted` with `reaction` null and clicks nothing, and after a click
  it runs out the wait and gives `outcome_unknown`.
- `comment_on_post` statuses: `commented`, `comments_disabled`,
  `post_unavailable`, `comment_failed`, `outcome_unknown`. `commented` relies
  on the text appearing on the page: after Post is pressed, the editor has to
  empty and the comment's first 60 characters have to render outside it. A
  comment LinkedIn accepts but shows differently reads `outcome_unknown`.
- `comment_on_post` refuses blank text and any control character, line breaks
  and tabs included, as a tool error before a browser is used, the same rule
  `send_message` applies.
- `get_connection_state` states are `detect_connection_state()`'s, read without
  a click: `pending`, `already_connected`, `connectable`, `follow_only`,
  `incoming_request`, `unavailable`, and `self_profile` for your own profile.
  `follow_only` can hide a Connect button that sits under the More menu:
  this tool does not open that menu, and `connect_with_person` does.
- `get_post_author` gives `status` `ok` when an author was read and
  `unreadable` when the post could not be read (then `name` and `username` are
  null). `username` is null for a company post. On a repost it reads the
  original post's author, not the member who reposted it.

`post_url` takes `/feed/update/<urn>/`, `/posts/<slug>`, either as a relative
path, or a bare `urn:li:activity|share|ugcPost:<id>`
(`identifiers.normalize_post_url`).

The transport's own `outcome_unknown` (`retry_safe: false`) still applies to
the two write tools, like every other tool that is not `readOnlyHint`.

## How a post is checked before a click

- The address has to stay a post permalink: after the load, after the post
  appears, and again inside every click program. A post that moves itself to
  `/feed/` after loading is `post_unavailable` with nothing clicked.
- When the URL names a post id (the urn's number, or the `activity-<id>` in a
  `/posts/` slug), the react button's nearest `[data-urn]` container has to
  carry that id in an attribute of its own or of an element inside it.
  Otherwise the result is `post_unavailable`, `retry_safe` true.
- The react button has to sit in an action bar that holds no other
  `aria-pressed` button (a Follow toggle in the header fails this), and
  hovering it has to open a menu with all six reaction types.

## Selectors

`scraping/engage_actions.py` explains each one. They follow the AGENTS.md
rules, so no decision reads a label:

- reactions are told apart by `data-test-reactions-icon-type`
- the react button is found by the three checks above
- the comment editor is the first visible `[role=textbox][contenteditable]` in
  `<main>`, and its Post button is the one `button[type=submit]` of its form
- the Comment button is the next plain button after the validated react
  button in its action bar
- the author comes from the first `/in/` or `/company/` link holding two or
  more `aria-hidden` text parts (the actor block's name and headline), else
  the first such link

The test pages in `tests/fixtures/engage/` are hand-built from LinkedIn's known
post-page structure, not saved from a live session. They are a claim about
LinkedIn's markup until the live check below confirms it, and that step saves
a real post page for the fixtures.

Two things only the live check can settle:

- whether `data-test-reactions-icon-type` ships in production and shows on the
  react button once pressed. Without it, every reaction ends in
  `outcome_unknown` or `react_failed`, and nothing is reported as done.
- whether a `urn:li:share:` post's container carries the share id. If it
  carries only the activity id, a share urn reads `post_unavailable`.

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
`test_the_owner_entry_point_says_so_before_it_can_fail`.

The browser tests in `tests/scraping/test_engage_actions_dom.py` passed, 11 of
11, before that restriction. The cases added since (20 in all now) have not
been run in a browser. `tests/scraping/test_engage_actions.py` covers the same
decisions without a browser.

## Live check

Not run yet. It needs Noor signed in with `--login`, on his own post
`urn:li:share:7510371678175096832`: `react_to_post` with `like`, then
`comment_on_post` with "Test, removing this now.", each checked on LinkedIn
and undone by hand. Save the post page's HTML as a fixture at the same time.
