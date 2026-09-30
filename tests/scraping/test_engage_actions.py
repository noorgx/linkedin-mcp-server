"""Decision tests for the engagement owner, without a browser.

``tests/scraping/test_engage_actions_dom.py`` runs the page programs against a
real DOM. Here every program is answered by name from a script, so these cases
hold the Python side: which answer leads to which click, when a click is
withheld, and what each result reports.
"""

from __future__ import annotations

from collections.abc import Callable
import re
from typing import Any, cast

import pytest
from patchright.async_api import Page
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

import linkedin_mcp_server.scraping.engage_actions as engage
from linkedin_mcp_server.scraping.engage_actions import EngageActions
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

POST = "https://www.linkedin.com/feed/update/urn:li:activity:7123456789012345678/"
POST_ID = "7123456789012345678"

PROGRAMS = {
    "candidates": engage.TRIGGER_CANDIDATES_JS,
    "retag": engage.RETAG_TRIGGER_JS,
    "menu_open": engage.MENU_OPEN_JS,
    "state": engage.POST_STATE_JS,
    "pick": engage.CLICK_REACTION_JS,
    "comment_button": engage.CLICK_COMMENT_BUTTON_JS,
    "write": engage.WRITE_COMMENT_JS,
    "clear": engage.CLEAR_COMMENT_JS,
    "submit": engage.SUBMIT_COMMENT_JS,
    "comment_state": engage.COMMENT_STATE_JS,
    "author": engage.POST_AUTHOR_JS,
}
_BY_SOURCE = {source: name for name, source in PROGRAMS.items()}


_REF = re.compile(r'data-engage-ref="([^"]+)"')


class FakeLocator:
    """A locator; a tagged button is named by its ref in the calls."""

    def __init__(self, page: FakePage, selector: str):
        self._page = page
        self._selector = selector
        match = _REF.search(selector)
        self._ref = match.group(1) if match else None

    async def count(self) -> int:
        if self._selector == "main":
            return 1
        if self._ref is not None:
            return 0 if self._ref in self._page.gone else 1
        return self._page.visible_editors

    async def hover(self, *, timeout: int | None = None) -> None:
        self._page.calls.append(("hover", self._ref))
        if self._page.on_hover is not None:
            self._page.on_hover(self._page)

    async def click(self, *, timeout: int | None = None) -> None:
        self._page.calls.append(("click", self._ref))
        if self._page.on_click is not None:
            self._page.on_click(self._page)


class FakePage:
    """Answers each page program from a per-name script.

    A list answers one call per item and repeats its last item; a callable is
    called with the argument.
    """

    def __init__(self, answers: dict[str, Any], *, url: str = POST):
        self.url = url
        self.answers = answers
        self.calls: list[tuple[Any, ...]] = []
        self.visible_editors = 1
        self.on_hover: Callable[[FakePage], None] | None = None
        self.on_click: Callable[[FakePage], None] | None = None
        self.never_appear: set[str] = set()
        self.gone: set[str] = set()

    async def evaluate(self, program: str, arg: Any = None) -> Any:
        name = _BY_SOURCE[program]
        self.calls.append(("evaluate", name, arg))
        answer = self.answers[name]
        if callable(answer):
            return answer(arg)
        if isinstance(answer, list):
            return answer.pop(0) if len(answer) > 1 else answer[0]
        return answer

    async def wait_for_selector(self, selector: str, **_: Any) -> None:
        self.calls.append(("wait", selector))
        if selector in self.never_appear:
            raise PlaywrightTimeoutError(f"{selector} never appeared")

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def evaluated(self, name: str) -> list[Any]:
        return [call[2] for call in self.calls if call[:2] == ("evaluate", name)]

    def clicks(self) -> list[Any]:
        return [call for call in self.calls if call[0] == "click"] + [
            call
            for call in self.calls
            if call[0] == "evaluate"
            and (call[1] in {"pick", "comment_button"} or call[1:] == ("submit", True))
        ]


class FakeNavigator:
    def __init__(self, page: FakePage, landing: str | None = None):
        self._page = page
        self._landing = landing

    async def _navigate_to_page(self, url: str) -> None:
        self._page.url = self._landing or url


def _actions(page: FakePage, landing: str | None = None) -> EngageActions:
    session = ScrapingSession(cast(Page, page))
    return EngageActions(session, cast(PageNavigator, FakeNavigator(page, landing)))


@pytest.fixture(autouse=True)
def short_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engage, "_new_token", lambda: "t")
    monkeypatch.setattr(engage, "_MENU_TIMEOUT", 0.2)
    monkeypatch.setattr(engage, "_CONFIRM_TIMEOUT", 0.2)
    monkeypatch.setattr(engage, "_SUBMIT_READY_TIMEOUT", 0.2)
    monkeypatch.setattr(engage, "_POLL", 0.01)


def _unpressed() -> dict[str, Any]:
    return {"hasTrigger": True, "pressed": False, "currentType": None}


def _pressed(reaction_type: str | None) -> dict[str, Any]:
    return {"hasTrigger": True, "pressed": True, "currentType": reaction_type}


class TestTrigger:
    async def test_the_first_candidate_whose_menu_opens_is_the_one_clicked(self):
        # A menu opens only while the second candidate is hovered, as on a
        # page where the first is a header toggle.
        page = FakePage(
            {
                "candidates": [["t-0", "t-1"]],
                "state": [_unpressed(), _pressed("LIKE")],
            }
        )
        page.answers["menu_open"] = lambda _: (
            [call for call in page.calls if call[0] == "hover"][-1] == ("hover", "t-1")
        )

        result = await _actions(page).react_to_post(POST, "like")

        assert result["status"] == "reacted"
        assert [c for c in page.calls if c[0] in {"hover", "click"}] == [
            ("hover", "t-0"),
            ("hover", "t-1"),
            ("click", "t-1"),
        ]
        assert page.evaluated("state") == ["t-1", "t-1"]

    async def test_no_validated_trigger_is_unavailable_and_clicks_nothing(self):
        page = FakePage({"candidates": [[]]})

        result = await _actions(page).react_to_post(POST, "like")

        assert result == {
            "url": POST,
            "status": "post_unavailable",
            "reaction": "like",
            "retry_safe": True,
        }
        assert page.clicks() == []

    async def test_a_trigger_whose_menu_never_opens_is_not_used(self):
        page = FakePage({"candidates": [["t-0"]], "menu_open": [False]})

        result = await _actions(page).react_to_post(POST, "celebrate")

        assert result["status"] == "post_unavailable"
        assert result["retry_safe"] is True
        assert page.clicks() == []

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (POST, POST_ID),
            (
                "https://www.linkedin.com/feed/update/urn:li:share:7510371678175096832/",
                "7510371678175096832",
            ),
            (
                "https://www.linkedin.com/posts/jane_ai-activity-7123456789012345678-AbCd",
                POST_ID,
            ),
            ("https://www.linkedin.com/posts/jane_ai-note", None),
        ],
    )
    async def test_the_requested_post_id_reaches_the_candidate_probe(
        self, url: str, expected: str | None
    ):
        page = FakePage({"candidates": [[]]})

        await _actions(page).react_to_post(url, "like")

        assert page.evaluated("candidates") == [{"postId": expected, "token": "t"}]


class TestConfirmation:
    async def test_like_without_a_readable_type_is_unknown_not_reacted(self):
        page = FakePage(
            {
                "candidates": [["t-0"]],
                "menu_open": [True],
                "state": [_unpressed(), _pressed(None)],
            }
        )

        result = await _actions(page).react_to_post(POST, "like")

        assert result["status"] == "outcome_unknown"
        assert result["retry_safe"] is False
        assert [c for c in page.calls if c[0] == "click"] == [("click", "t-0")]

    async def test_a_switch_that_shows_no_type_is_unknown_not_reacted(self):
        page = FakePage(
            {
                "candidates": [["t-0"]],
                "menu_open": [True],
                "state": [_pressed("PRAISE"), _pressed(None)],
                "pick": ["clicked"],
            }
        )

        result = await _actions(page).react_to_post(POST, "insightful")

        assert result["status"] == "outcome_unknown"
        assert result["retry_safe"] is False
        assert page.evaluated("pick") == [{"ref": "t-0", "type": "INTEREST"}]

    async def test_a_switch_that_shows_the_old_type_is_unknown_not_reacted(self):
        page = FakePage(
            {
                "candidates": [["t-0"]],
                "menu_open": [True],
                "state": [_pressed("PRAISE")],
                "pick": ["clicked"],
            }
        )

        result = await _actions(page).react_to_post(POST, "love")

        assert result["status"] == "outcome_unknown"

    async def test_like_already_pressed_clicks_nothing(self):
        page = FakePage(
            {"candidates": [["t-0"]], "menu_open": [True], "state": [_pressed("LIKE")]}
        )

        result = await _actions(page).react_to_post(POST, "like")

        assert result["status"] == "already_reacted"
        assert result["retry_safe"] is True
        assert page.clicks() == []

    async def test_a_pressed_trigger_with_no_readable_type_clicks_nothing(self):
        page = FakePage(
            {"candidates": [["t-0"]], "menu_open": [True], "state": [_pressed(None)]}
        )

        result = await _actions(page).react_to_post(POST, "celebrate")

        assert result == {
            "url": POST,
            "status": "already_reacted",
            "reaction": None,
            "retry_safe": True,
        }
        assert page.clicks() == []


class TestTag:
    """The validated button is found by its tag, never by its position."""

    async def test_a_tag_gone_before_the_like_click_clicks_nothing(self):
        page = FakePage(
            {"candidates": [["t-0"]], "menu_open": [True], "state": [_unpressed()]}
        )
        page.gone = {"t-0"}

        result = await _actions(page).react_to_post(POST, "like")

        assert result == {
            "url": POST,
            "status": "post_unavailable",
            "reaction": "like",
            "retry_safe": True,
        }
        assert page.clicks() == []

    async def test_a_tag_gone_before_the_menu_pick_clicks_nothing(self):
        page = FakePage(
            {
                "candidates": [["t-0"]],
                "menu_open": [True],
                "state": [_unpressed()],
                "pick": ["gone"],
            }
        )

        result = await _actions(page).react_to_post(POST, "celebrate")

        assert result["status"] == "post_unavailable"
        assert result["retry_safe"] is True

    async def test_a_tag_gone_before_the_comment_button_is_unavailable(self):
        page = FakePage(
            {"candidates": [["t-0"]], "menu_open": [True], "comment_button": ["gone"]}
        )
        page.visible_editors = 0

        result = await _actions(page).comment_on_post(POST, "Useful point.")

        assert result == {"url": POST, "status": "post_unavailable", "retry_safe": True}
        assert page.evaluated("write") == []

    async def test_a_button_redrawn_after_the_click_is_tagged_again(self):
        # LinkedIn may replace the button when the reaction lands. The one
        # button that still passes the bar and post checks takes the tag back
        # and its state confirms the reaction.
        page = FakePage(
            {
                "candidates": [["t-0"]],
                "menu_open": [True],
                "state": [
                    _unpressed(),
                    {"hasTrigger": False},
                    _pressed("LIKE"),
                ],
                "retag": [True],
            }
        )

        result = await _actions(page).react_to_post(POST, "like")

        assert result["status"] == "reacted"
        assert page.evaluated("retag") == [{"postId": POST_ID, "ref": "t-0"}]

    async def test_a_redrawn_button_that_cannot_be_tagged_is_unknown(self):
        page = FakePage(
            {
                "candidates": [["t-0"]],
                "menu_open": [True],
                "state": [_unpressed(), {"hasTrigger": False}],
                "retag": [False],
            }
        )

        result = await _actions(page).react_to_post(POST, "like")

        assert result["status"] == "outcome_unknown"
        assert result["retry_safe"] is False


def _moves_to_feed(page: FakePage) -> None:
    page.url = "https://www.linkedin.com/feed/"


class TestAddress:
    async def test_a_move_off_the_post_after_load_stops_the_like_click(self):
        page = FakePage(
            {"candidates": [["t-0"]], "menu_open": [True], "state": [_unpressed()]}
        )
        page.on_hover = _moves_to_feed

        result = await _actions(page).react_to_post(POST, "like")

        assert result == {
            "url": POST,
            "status": "post_unavailable",
            "reaction": "like",
            "retry_safe": True,
        }
        assert page.clicks() == []

    async def test_a_move_after_the_trigger_is_validated_stops_the_click(self):
        # The last check before the click, after the trigger passed its own.
        def state_then_move(_ref: str) -> dict[str, Any]:
            _moves_to_feed(page)
            return _unpressed()

        page = FakePage(
            {"candidates": [["t-0"]], "menu_open": [True], "state": state_then_move}
        )

        result = await _actions(page).react_to_post(POST, "like")

        assert result["status"] == "post_unavailable"
        assert result["retry_safe"] is True
        assert page.clicks() == []

    async def test_a_move_off_the_post_after_load_stops_the_menu_pick(self):
        page = FakePage(
            {"candidates": [["t-0"]], "menu_open": [True], "state": [_unpressed()]}
        )
        page.on_hover = _moves_to_feed

        result = await _actions(page).react_to_post(POST, "celebrate")

        assert result["status"] == "post_unavailable"
        assert page.clicks() == []

    async def test_a_menu_pick_the_page_refuses_after_a_move_is_unavailable(self):
        # The click programs re-check the address themselves, so a move that
        # lands between the Python check and the click still clicks nothing.
        page = FakePage(
            {
                "candidates": [["t-0"]],
                "menu_open": [True],
                "state": [_unpressed()],
                "pick": ["moved"],
            }
        )

        result = await _actions(page).react_to_post(POST, "celebrate")

        assert result["status"] == "post_unavailable"
        assert result["retry_safe"] is True

    async def test_a_post_that_loads_on_the_feed_is_unavailable(self):
        page = FakePage({})

        result = await _actions(
            page, landing="https://www.linkedin.com/feed/"
        ).react_to_post(POST, "like")

        assert result["status"] == "post_unavailable"
        assert page.calls == [("wait", "main")]

    async def test_a_move_before_submit_posts_nothing_and_clears_the_text(self):
        def write(_text: str) -> str:
            _moves_to_feed(page)
            return "written"

        page = FakePage(
            {
                "candidates": [["t-0"]],
                "menu_open": [True],
                "write": write,
                "clear": [True],
                "submit": ["ready"],
            }
        )

        result = await _actions(page).comment_on_post(POST, "Useful point.")

        assert result == {"url": POST, "status": "post_unavailable", "retry_safe": True}
        assert page.evaluated("submit") == []
        assert page.evaluated("clear") == [None]


class TestComment:
    async def test_a_comment_that_never_renders_is_unknown(self):
        page = FakePage(
            {
                "candidates": [["t-0"]],
                "menu_open": [True],
                "write": ["written"],
                "submit": lambda click: "clicked" if click else "ready",
                "comment_state": [{"shown": 0, "editorEmpty": False}],
            }
        )

        result = await _actions(page).comment_on_post(POST, "Useful point.")

        assert result == {"url": POST, "status": "outcome_unknown", "retry_safe": False}
        assert page.evaluated("submit").count(True) == 1

    async def test_no_editor_after_the_comment_button_is_comments_disabled(self):
        page = FakePage(
            {
                "candidates": [["t-0"]],
                "menu_open": [True],
                "comment_button": ["clicked"],
            }
        )
        page.visible_editors = 0
        page.never_appear = {engage._EDITOR}

        result = await _actions(page).comment_on_post(POST, "Useful point.")

        assert result == {
            "url": POST,
            "status": "comments_disabled",
            "retry_safe": True,
        }
        assert page.evaluated("comment_button") == ["t-0"]
        assert page.evaluated("write") == []


class TestAuthor:
    async def test_a_read_author_is_ok(self):
        page = FakePage(
            {"author": [{"kind": "in", "slug": "jane-doe", "name": "Jane Doe"}]}
        )

        result = await _actions(page).get_post_author(POST)

        assert result == {
            "url": POST,
            "status": "ok",
            "name": "Jane Doe",
            "username": "jane-doe",
        }

    async def test_a_page_with_no_author_is_unreadable(self):
        page = FakePage({"author": [None]})

        result = await _actions(page).get_post_author(POST)

        assert result == {
            "url": POST,
            "status": "unreadable",
            "name": None,
            "username": None,
        }

    async def test_a_post_that_loads_elsewhere_is_unreadable(self):
        page = FakePage({})

        result = await _actions(
            page, landing="https://www.linkedin.com/feed/"
        ).get_post_author(POST)

        assert result["status"] == "unreadable"
