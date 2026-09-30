"""Tool-level contract for the engagement tools.

The page work lives in ``scraping/engage_actions.py`` and is covered against a
real DOM in ``tests/scraping/test_engage_actions_dom.py``. Here the extractor is
a mock: these cases hold what a client sees (names, safety annotations,
argument repair) and that a bad argument is refused before a browser is
acquired.
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.tools import FunctionTool

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping.identifiers import normalize_post_url

ENGAGE_TOOLS = {
    "react_to_post": "destructive_hint",
    "comment_on_post": "destructive_hint",
    "get_connection_state": "read_only_hint",
    "get_post_author": "read_only_hint",
}

POST = "https://www.linkedin.com/feed/update/urn:li:activity:7123456789012345678/"


async def _tool_fn(
    mcp: FastMCP, name: str
) -> Callable[..., Coroutine[Any, Any, dict[str, Any]]]:
    tool = await mcp.get_tool(name)
    assert tool is not None, name
    return cast(FunctionTool, tool).fn


@pytest.fixture
def engage_mcp() -> FastMCP:
    from linkedin_mcp_server.tools.engage import register_engage_tools

    mcp = FastMCP("test")
    register_engage_tools(mcp)
    return mcp


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch) -> Callable[[Any], AsyncMock]:
    def _serve(extractor: Any) -> AsyncMock:
        ready = AsyncMock(return_value=extractor)
        monkeypatch.setattr(
            "linkedin_mcp_server.tools.engage.get_ready_extractor", ready
        )
        return ready

    return _serve


class TestRegistration:
    async def test_the_server_offers_the_four_tools_with_their_safety_hints(self):
        from linkedin_mcp_server.server import create_mcp_server

        mcp = create_mcp_server()
        for name, hint in ENGAGE_TOOLS.items():
            tool = await mcp.get_tool(name)
            assert tool is not None, name
            annotations = tool.annotations
            assert annotations is not None
            assert getattr(annotations, hint) is True, name
            other = (
                "read_only_hint" if hint == "destructive_hint" else "destructive_hint"
            )
            assert getattr(annotations, other) is not True, name
            assert "engage" in tool.tags

    async def test_the_tools_take_the_contract_arguments(self, engage_mcp):
        expected = {
            "react_to_post": {"post_url", "reaction"},
            "comment_on_post": {"post_url", "text"},
            "get_connection_state": {"linkedin_username"},
            "get_post_author": {"post_url"},
        }
        for name, arguments in expected.items():
            tool = await engage_mcp.get_tool(name)
            assert tool is not None
            assert set(tool.parameters["properties"]) == arguments, name
            assert set(tool.parameters["required"]) == arguments, name


class TestPostUrl:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (POST, POST),
            ("/feed/update/urn:li:activity:7123456789012345678/", POST),
            ("linkedin.com/feed/update/urn:li:activity:7123456789012345678", POST),
            (
                "https://www.linkedin.com/feed/update/urn%3Ali%3Aactivity%3A7123456789012345678/?trk=x",
                POST,
            ),
            ("urn:li:activity:7123456789012345678", POST),
            (
                "urn:li:share:7510371678175096832",
                "https://www.linkedin.com/feed/update/urn:li:share:7510371678175096832/",
            ),
            (
                "https://www.linkedin.com/posts/jane-doe_ai-activity-7123456789012345678-AbCd?utm_source=share",
                "https://www.linkedin.com/posts/jane-doe_ai-activity-7123456789012345678-AbCd",
            ),
            (
                "/posts/jane-doe_ai-activity-7123456789012345678-AbCd/",
                "https://www.linkedin.com/posts/jane-doe_ai-activity-7123456789012345678-AbCd",
            ),
        ],
    )
    def test_both_permalink_shapes_and_relative_paths_normalize(
        self, value: str, expected: str
    ):
        assert normalize_post_url(value) == expected

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "/feed/",
            "https://www.linkedin.com/in/jane-doe/",
            "https://example.com/feed/update/urn:li:activity:1/",
            "/feed/update/urn:li:activity:abc/",
            "/feed/update/urn:li:activity:1/../../../in/x/",
            "/posts/",
        ],
    )
    def test_anything_else_is_refused(self, value: str):
        with pytest.raises(InvalidReferenceError):
            normalize_post_url(value)


class TestToolBodies:
    async def test_react_passes_the_normalized_url_and_reaction(
        self, engage_mcp, serve, mock_context
    ):
        result = {
            "url": POST,
            "status": "reacted",
            "reaction": "celebrate",
            "retry_safe": False,
        }
        extractor = MagicMock()
        extractor.react_to_post = AsyncMock(return_value=result)
        serve(extractor)

        fn = await _tool_fn(engage_mcp, "react_to_post")
        answer = await fn(
            "/feed/update/urn:li:activity:7123456789012345678/",
            " Celebrate ",
            mock_context,
        )

        assert answer == result
        extractor.react_to_post.assert_awaited_once_with(POST, "celebrate")

    async def test_an_unknown_reaction_is_refused_before_a_browser(
        self, engage_mcp, serve, mock_context
    ):
        ready = serve(MagicMock())
        fn = await _tool_fn(engage_mcp, "react_to_post")

        with pytest.raises(
            ToolError, match="like, celebrate, support, love, insightful, funny"
        ):
            await fn(POST, "clap", mock_context)
        ready.assert_not_awaited()

    async def test_a_bad_post_url_is_refused_before_a_browser(
        self, engage_mcp, serve, mock_context
    ):
        ready = serve(MagicMock())
        for name, args in (
            ("react_to_post", ("/in/jane/", "like")),
            ("comment_on_post", ("/in/jane/", "Nice point on caching.")),
            ("get_post_author", ("/in/jane/",)),
        ):
            fn = await _tool_fn(engage_mcp, name)
            with pytest.raises(ToolError, match="post"):
                await fn(*args, mock_context)
        ready.assert_not_awaited()

    async def test_comment_passes_the_text_unchanged(
        self, engage_mcp, serve, mock_context
    ):
        text = "Caching at the edge cut our p95 in half. @Jane #infra"
        result = {"url": POST, "status": "commented", "retry_safe": False}
        extractor = MagicMock()
        extractor.comment_on_post = AsyncMock(return_value=result)
        serve(extractor)

        fn = await _tool_fn(engage_mcp, "comment_on_post")
        assert await fn(POST, text, mock_context) == result
        extractor.comment_on_post.assert_awaited_once_with(POST, text)

    @pytest.mark.parametrize("text", ["", "   ", "line one\nline two", "tab\there"])
    async def test_an_unusable_comment_is_refused_before_a_browser(
        self, engage_mcp, serve, mock_context, text: str
    ):
        ready = serve(MagicMock())
        fn = await _tool_fn(engage_mcp, "comment_on_post")

        with pytest.raises(ToolError, match="Comment"):
            await fn(POST, text, mock_context)
        ready.assert_not_awaited()

    async def test_connection_state_passes_the_username(
        self, engage_mcp, serve, mock_context
    ):
        result = {"url": "https://www.linkedin.com/in/jane-doe/", "state": "pending"}
        extractor = MagicMock()
        extractor.get_connection_state = AsyncMock(return_value=result)
        serve(extractor)

        fn = await _tool_fn(engage_mcp, "get_connection_state")
        assert await fn("https://www.linkedin.com/in/jane-doe/", mock_context) == result
        extractor.get_connection_state.assert_awaited_once_with("jane-doe")

    async def test_post_author_passes_the_normalized_url(
        self, engage_mcp, serve, mock_context
    ):
        result = {"url": POST, "name": "Jane Doe", "username": "jane-doe"}
        extractor = MagicMock()
        extractor.get_post_author = AsyncMock(return_value=result)
        serve(extractor)

        fn = await _tool_fn(engage_mcp, "get_post_author")
        assert await fn("urn:li:activity:7123456789012345678", mock_context) == result
        extractor.get_post_author.assert_awaited_once_with(POST)
