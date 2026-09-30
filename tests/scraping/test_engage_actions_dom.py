"""Browser-DOM tests for the engagement actions.

The engagement owners run their page programs against the saved fixtures in
``tests/fixtures/engage/`` in headless chromium. Each fixture is filled from
one of four label sets (English, German, opaque tokens, and present-but-empty
ARIA values) and every case asserts one answer for all four, the same
discipline ``tests/test_action_signals_dom.py`` applies to the connect flow: a
decision that differs between two sets read a word, which the AGENTS.md
Scraping Rules forbid.

The fixture pages are hand-built from LinkedIn's known post-page structure,
not saved from a live session; the live check is where a real page gets saved.

No page here is on LinkedIn. The navigator stand-in serves the fixture from
``http://fixture.test`` under the path the action asked for, so the
redirect guard sees a real address, and no request leaves the browser.

Clicks are recorded by a capturing listener in the fixture, on
``<body data-clicks>``, by fixture id. Hovering is not a click.

Skipped automatically when chromium is not installed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from string import Template
from typing import Any, cast
from urllib.parse import urlparse

import pytest
from patchright.async_api import Page, async_playwright

from linkedin_mcp_server.scraping.connection_actions import ConnectionActions
from linkedin_mcp_server.scraping.engage_actions import EngageActions
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "engage"
POST = "https://www.linkedin.com/feed/update/urn:li:activity:7123456789012345678/"
COMMENT = "Caching at the edge cut our p95 in half, @Jane #infra"


@dataclass(frozen=True, slots=True)
class Labels:
    locale: str
    home: str
    avatar_alt: str
    follow: str
    counts: str
    react: str
    like: str
    celebrate: str
    support: str
    love: str
    insightful: str
    funny: str
    comment: str
    repost: str
    send: str
    placeholder: str
    emoji: str
    post: str
    message: str
    pending: str
    more: str
    connect: str


ENGLISH = Labels(
    locale="en",
    home="Home",
    avatar_alt="View Jane Doe's profile",
    follow="Follow Jane Doe",
    counts="42 reactions",
    react="React Like",
    like="Like",
    celebrate="Celebrate",
    support="Support",
    love="Love",
    insightful="Insightful",
    funny="Funny",
    comment="Comment",
    repost="Repost",
    send="Send",
    placeholder="Add a comment...",
    emoji="Open Emoji Keyboard",
    post="Comment",
    message="Message",
    pending="Pending, click to withdraw invitation sent to Florian",
    more="More",
    connect="Invite Someone to connect",
)

GERMAN = Labels(
    locale="de",
    home="Startseite",
    avatar_alt="Profil von Jane Doe anzeigen",
    follow="Jane Doe folgen",
    counts="42 Reaktionen",
    react="Reagieren: Gefällt mir",
    like="Gefällt mir",
    celebrate="Glückwunsch",
    support="Unterstützen",
    love="Wunderbar",
    insightful="Inspirierend",
    funny="Lustig",
    comment="Kommentieren",
    repost="Reposten",
    send="Senden",
    placeholder="Kommentar hinzufügen …",
    emoji="Emoji-Tastatur öffnen",
    post="Kommentieren",
    message="Nachricht",
    pending="Ausstehend, klicken zum Zurückziehen",
    more="Mehr",
    connect="Als Kontakt einladen",
)

OPAQUE = Labels(
    locale="opaque",
    home="q1",
    avatar_alt="q2",
    follow="q3",
    counts="q4",
    react="q5",
    like="q6",
    celebrate="q7",
    support="q8",
    love="q9",
    insightful="q10",
    funny="q11",
    comment="q12",
    repost="q13",
    send="q14",
    placeholder="q15",
    emoji="q16",
    post="q17",
    message="q18",
    pending="q19",
    more="q20",
    connect="q21",
)

EMPTY_ARIA = Labels(
    locale="empty-aria",
    **{name: "" for name in asdict(ENGLISH) if name != "locale"},
)

LOCALES = (ENGLISH, GERMAN, OPAQUE, EMPTY_ARIA)


def _render(
    name: str,
    labels: Labels,
    *,
    comment_box_hidden: bool = False,
    draft: str = "",
    actor_href: str = "https://www.linkedin.com/in/jane-doe?miniProfileUrn=urn%3Ali%3Afs_miniProfile%3AAAA",
    actor_name: str = "Jane Doe",
    already: str | None = None,
    follow_pressed: bool = False,
    post_urn: str = "urn:li:activity:7123456789012345678",
    repost_header: str = "",
    silent: bool = False,
    comments_enabled: bool = True,
    move_on_hover: bool = False,
) -> str:
    """Fill a fixture. ``already`` is a reaction type the post carries on
    load, or ``"unreadable"`` for a pressed trigger whose icon names none."""
    values = {k: v for k, v in asdict(labels).items() if k != "locale"}
    values.update(
        comment_box_hidden="hidden" if comment_box_hidden else "",
        draft=draft,
        actor_href=actor_href,
        actor_name=actor_name,
        actor_headline="Platform engineer",
        body="We moved our cache to the edge.",
        existing_comment="Great write-up.",
        pressed="true" if already else "false",
        trigger_icon=(
            f'<img data-test-reactions-icon-type="{already}" alt="">'
            if already and already != "unreadable"
            else '<svg aria-hidden="true"></svg>'
        ),
        follow_attrs='aria-pressed="false"' if follow_pressed else "",
        post_urn=post_urn,
        repost_header=repost_header,
        silent="true" if silent else "false",
        comments_enabled="true" if comments_enabled else "false",
        move_on_hover="true" if move_on_hover else "false",
    )
    return Template((FIXTURES / name).read_text(encoding="utf-8")).substitute(values)


class FixtureNavigator:
    """Serve one fixture at the path the action navigates to.

    ``serve_at`` replaces that path, which is how a post LinkedIn no longer
    serves (a redirect to the feed) is modelled.
    """

    def __init__(self, page: Any, html: str, *, serve_at: str | None = None):
        self._page = page
        self._html = html
        self._serve_at = serve_at
        self.visited: list[str] = []

    async def _navigate_to_page(self, url: str) -> None:
        self.visited.append(url)

        async def fulfil(route):
            await route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=self._html
            )

        await self._page.route("http://fixture.test/**", fulfil)
        path = self._serve_at or urlparse(url).path
        await self._page.goto(f"http://fixture.test{path}")


@pytest.fixture
async def dom_page():
    """Real chromium page, or skip when no browser is installed."""
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield page
        finally:
            await browser.close()


def _engage(page: Any, html: str, **navigator: Any) -> EngageActions:
    session = ScrapingSession(cast(Page, page))
    return EngageActions(
        session, cast(PageNavigator, FixtureNavigator(page, html, **navigator))
    )


async def _clicks(page: Any) -> list[str]:
    value = await page.evaluate("document.body.getAttribute('data-clicks')")
    return [item for item in (value or "").split(",") if item]


async def _react(
    page: Any, labels: Labels, reaction: str, **variant: Any
) -> tuple[dict[str, Any], list[str], str | None]:
    html = _render("post.html", labels, **variant)
    actions = _engage(page, html)
    result = await actions.react_to_post(POST, reaction)
    now = await page.evaluate(
        """() => {
            const trigger = document.querySelector('[data-fixture-id="react-trigger"]');
            if (trigger.getAttribute('aria-pressed') !== 'true') return null;
            const icon = trigger.querySelector('[data-test-reactions-icon-type]');
            return icon && icon.getAttribute('data-test-reactions-icon-type');
        }"""
    )
    return result, await _clicks(page), now


async def _every_locale(page: Any, read) -> dict[str, Any]:
    return {labels.locale: await read(page, labels) for labels in LOCALES}


def _same(expected: Any) -> dict[str, Any]:
    return {labels.locale: expected for labels in LOCALES}


class TestReact:
    async def test_the_menu_opens_and_the_requested_reaction_is_chosen(self, dom_page):
        # The counts button ahead of the bar carries a PRAISE icon too; only
        # the six-type menu may be clicked.
        answers = await _every_locale(
            dom_page, lambda page, labels: _react(page, labels, "celebrate")
        )
        assert answers == _same(
            (
                {
                    "url": POST,
                    "status": "reacted",
                    "reaction": "celebrate",
                    "retry_safe": False,
                },
                ["menu-PRAISE"],
                "PRAISE",
            )
        )

    async def test_like_is_one_click_on_the_trigger(self, dom_page):
        answers = await _every_locale(
            dom_page, lambda page, labels: _react(page, labels, "like")
        )
        assert answers == _same(
            (
                {
                    "url": POST,
                    "status": "reacted",
                    "reaction": "like",
                    "retry_safe": False,
                },
                ["react-trigger"],
                "LIKE",
            )
        )

    async def test_the_same_reaction_again_clicks_nothing(self, dom_page):
        answers = await _every_locale(
            dom_page,
            lambda page, labels: _react(page, labels, "celebrate", already="PRAISE"),
        )
        assert answers == _same(
            (
                {
                    "url": POST,
                    "status": "already_reacted",
                    "reaction": "celebrate",
                    "retry_safe": True,
                },
                [],
                "PRAISE",
            )
        )

    async def test_a_different_reaction_switches_through_the_menu(self, dom_page):
        # Clicking the pressed trigger would take the reaction away, so a
        # switch to like goes through the menu as well.
        answers = await _every_locale(
            dom_page,
            lambda page, labels: _react(page, labels, "like", already="PRAISE"),
        )
        assert answers == _same(
            (
                {
                    "url": POST,
                    "status": "reacted",
                    "reaction": "like",
                    "retry_safe": False,
                },
                ["menu-LIKE"],
                "LIKE",
            )
        )

    async def test_a_pressed_follow_above_the_bar_is_never_the_trigger(self, dom_page):
        # Follow carries aria-pressed and comes first in the document; like
        # still lands on the post's own trigger, and Follow is never clicked.
        answers = await _every_locale(
            dom_page,
            lambda page, labels: _react(page, labels, "like", follow_pressed=True),
        )
        assert answers == _same(
            (
                {
                    "url": POST,
                    "status": "reacted",
                    "reaction": "like",
                    "retry_safe": False,
                },
                ["react-trigger"],
                "LIKE",
            )
        )

    async def test_like_already_in_place_clicks_nothing(self, dom_page):
        answers = await _every_locale(
            dom_page,
            lambda page, labels: _react(page, labels, "like", already="LIKE"),
        )
        assert answers == _same(
            (
                {
                    "url": POST,
                    "status": "already_reacted",
                    "reaction": "like",
                    "retry_safe": True,
                },
                [],
                "LIKE",
            )
        )

    async def test_a_pressed_trigger_with_no_readable_type_clicks_nothing(
        self, dom_page
    ):
        answers = await _every_locale(
            dom_page,
            lambda page, labels: _react(
                page, labels, "celebrate", already="unreadable"
            ),
        )
        assert answers == _same(
            (
                {
                    "url": POST,
                    "status": "already_reacted",
                    "reaction": None,
                    "retry_safe": True,
                },
                [],
                None,
            )
        )

    async def test_a_reaction_the_page_never_shows_is_unknown(self, dom_page):
        # One label set: the confirmation waits its full budget.
        result, clicks, now = await _react(dom_page, ENGLISH, "celebrate", silent=True)

        assert result == {
            "url": POST,
            "status": "outcome_unknown",
            "reaction": "celebrate",
            "retry_safe": False,
        }
        assert clicks == ["menu-PRAISE"]
        assert now is None

    async def test_a_move_to_the_feed_after_load_stops_before_any_click(self, dom_page):
        # The page rewrites its own address to /feed/ when the trigger is
        # hovered, keeping the post on screen. Nothing may be clicked.
        answers = await _every_locale(
            dom_page,
            lambda page, labels: _react(page, labels, "like", move_on_hover=True),
        )
        assert answers == _same(
            (
                {
                    "url": POST,
                    "status": "post_unavailable",
                    "reaction": "like",
                    "retry_safe": True,
                },
                [],
                None,
            )
        )

    async def test_a_post_whose_urn_does_not_match_is_left_alone(self, dom_page):
        answers = await _every_locale(
            dom_page,
            lambda page, labels: _react(
                page, labels, "like", post_urn="urn:li:activity:5555555555555555555"
            ),
        )
        assert answers == _same(
            (
                {
                    "url": POST,
                    "status": "post_unavailable",
                    "reaction": "like",
                    "retry_safe": True,
                },
                [],
                None,
            )
        )

    async def test_a_post_redirected_away_is_unavailable_and_untouched(self, dom_page):
        actions = _engage(dom_page, _render("post.html", ENGLISH), serve_at="/feed/")
        result = await actions.react_to_post(POST, "like")

        assert result == {
            "url": POST,
            "status": "post_unavailable",
            "reaction": "like",
            "retry_safe": True,
        }
        assert await _clicks(dom_page) == []


async def _comment(
    page: Any, labels: Labels, *, hidden: bool = False, draft: str = "", **variant: Any
) -> tuple[dict[str, Any], list[str], list[str], str]:
    html = _render(
        "post.html", labels, comment_box_hidden=hidden, draft=draft, **variant
    )
    result = await _engage(page, html).comment_on_post(POST, COMMENT)
    posted = await page.evaluate(
        "Array.from(document.querySelectorAll('.new-comment')).map(e => e.innerText)"
    )
    editor = await page.evaluate(
        "document.querySelector('[data-fixture-id=\"comment-editor\"]').innerText"
    )
    return result, await _clicks(page), posted, editor


class TestComment:
    async def test_the_box_receives_the_text_and_post_is_pressed_once(self, dom_page):
        answers = await _every_locale(
            dom_page, lambda page, labels: _comment(page, labels)
        )
        assert answers == _same(
            (
                {"url": POST, "status": "commented", "retry_safe": False},
                ["comment-submit"],
                [COMMENT],
                "",
            )
        )

    async def test_a_closed_box_is_opened_from_the_action_bar(self, dom_page):
        answers = await _every_locale(
            dom_page, lambda page, labels: _comment(page, labels, hidden=True)
        )
        assert answers == _same(
            (
                {"url": POST, "status": "commented", "retry_safe": False},
                ["comment-button", "comment-submit"],
                [COMMENT],
                "",
            )
        )

    async def test_a_post_that_opens_no_editor_is_comments_disabled(self, dom_page):
        answers = await _every_locale(
            dom_page,
            lambda page, labels: _comment(
                page, labels, hidden=True, comments_enabled=False
            ),
        )
        assert answers == _same(
            (
                {"url": POST, "status": "comments_disabled", "retry_safe": True},
                ["comment-button"],
                [],
                "",
            )
        )

    async def test_the_comment_button_is_found_past_a_pressed_follow(self, dom_page):
        answers = await _every_locale(
            dom_page,
            lambda page, labels: _comment(
                page, labels, hidden=True, follow_pressed=True
            ),
        )
        assert answers == _same(
            (
                {"url": POST, "status": "commented", "retry_safe": False},
                ["comment-button", "comment-submit"],
                [COMMENT],
                "",
            )
        )

    async def test_a_draft_in_the_box_is_left_alone(self, dom_page):
        answers = await _every_locale(
            dom_page,
            lambda page, labels: _comment(page, labels, draft="half-written thought"),
        )
        assert answers == _same(
            (
                {"url": POST, "status": "comment_failed", "retry_safe": True},
                [],
                [],
                "half-written thought",
            )
        )


class TestPostAuthor:
    async def test_the_author_slug_and_name_are_read(self, dom_page):
        async def read(page, labels):
            return await _engage(page, _render("post.html", labels)).get_post_author(
                POST
            )

        answers = await _every_locale(dom_page, read)
        assert answers == _same(
            {"url": POST, "status": "ok", "name": "Jane Doe", "username": "jane-doe"}
        )

    async def test_a_repost_is_credited_to_the_original_author(self, dom_page):
        # The reposter's header links come first in the document, with plain
        # text; the actor block below it is the author.
        async def read(page, labels):
            header = (
                '<div class="update-components-header">'
                '<a href="https://www.linkedin.com/in/reposter-person/">'
                '<img alt=""></a><span>'
                '<a href="https://www.linkedin.com/in/reposter-person/">'
                "Rae Poster</a> " + labels.repost + "</span></div>"
            )
            html = _render("post.html", labels, repost_header=header)
            return await _engage(page, html).get_post_author(POST)

        answers = await _every_locale(dom_page, read)
        assert answers == _same(
            {"url": POST, "status": "ok", "name": "Jane Doe", "username": "jane-doe"}
        )

    async def test_a_company_post_has_no_username(self, dom_page):
        html = _render(
            "post.html",
            ENGLISH,
            actor_href="https://www.linkedin.com/company/acme-robotics/posts",
            actor_name="Acme Robotics",
        )
        result = await _engage(dom_page, html).get_post_author(POST)

        assert result == {
            "url": POST,
            "status": "ok",
            "name": "Acme Robotics",
            "username": None,
        }


class TestConnectionState:
    async def test_pending_is_read_without_a_single_click(self, dom_page):
        async def read(page, labels):
            async def main_profile(username: str) -> dict[str, Any]:
                assert username == "testuser"
                return {"sections": {"main_profile": "Florian\nPending"}}

            await page.set_content(_render("profile_pending.html", labels))
            session = ScrapingSession(cast(Page, page))
            actions = ConnectionActions(session, PageNavigator(session), main_profile)
            result = await actions.get_connection_state("testuser")
            expanded = await page.evaluate(
                "document.querySelector('[data-fixture-id=\"more\"]')"
                ".getAttribute('aria-expanded')"
            )
            return result, await _clicks(page), expanded

        answers = await _every_locale(dom_page, read)
        assert answers == _same(
            (
                {"url": "https://www.linkedin.com/in/testuser/", "state": "pending"},
                [],
                "false",
            )
        )
