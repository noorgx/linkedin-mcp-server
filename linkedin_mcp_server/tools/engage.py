"""
LinkedIn engagement tools: react, comment, connection state and post author.

React and comment are writes, annotated with destructiveHint so MCP clients
ask before running them. The other two only read. Every argument is checked
before a browser session is acquired, so a mistyped reaction or a link that
is not a post costs no page load.
"""

import logging
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.core.exceptions import AuthenticationError
from linkedin_mcp_server.dependencies import get_ready_extractor, handle_auth_error
from linkedin_mcp_server.error_handler import raise_tool_error
from linkedin_mcp_server.scraping.engage_actions import (
    REACTION_TYPES,
    invalid_comment_reason,
)
from linkedin_mcp_server.scraping.identifiers import (
    normalize_person_identifier,
    normalize_post_url,
)

logger = logging.getLogger(__name__)

# The action has answered, and the progress notification is the last await
# inside FastMCP's deadline. A deadline landing there discards a result that may
# say the action happened, so this line is all that is left of it.
_RESULT_LOST_WARNING = (
    "%s acted on LinkedIn but its result was lost to the tool deadline. Check "
    "the post before calling again, as a repeat may act twice."
)


def register_engage_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the engagement tools with the MCP server."""

    @mcp.tool(
        timeout=tool_timeout,
        title="React To Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"engage"},
    )
    async def react_to_post(
        post_url: str,
        reaction: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """
        React to a LinkedIn post. This is a write operation.

        Args:
            post_url: Post permalink, "/feed/update/<urn>/" or "/posts/<slug>",
                as a full URL or a relative path, or the post's urn
            reaction: One of like, celebrate, support, love, insightful, funny
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, status, reaction and retry_safe.
            Statuses: reacted, already_reacted, post_unavailable, react_failed,
            outcome_unknown. ``already_reacted`` means the requested reaction
            was already in place and nothing was clicked; a different existing
            reaction is switched to the requested one and reports ``reacted``.
            ``retry_safe`` is false once a click was dispatched; on
            ``outcome_unknown`` check the post before calling again, because a
            repeated like click takes the reaction away.
        """
        try:
            post_url = normalize_post_url(post_url)
            reaction = reaction.strip().lower()
            if reaction not in REACTION_TYPES:
                raise ToolError(
                    "reaction must be one of: " + ", ".join(REACTION_TYPES) + "."
                )
            extractor = await get_ready_extractor(ctx, tool_name="react_to_post")
            logger.info("Reacting to %s with %s", post_url, reaction)
            await ctx.report_progress(progress=0, total=100, message="Reacting")

            result = await extractor.react_to_post(post_url, reaction)

            try:
                await ctx.report_progress(progress=100, total=100, message="Complete")
            except BaseException:
                if result.get("retry_safe") is False:
                    logger.warning(_RESULT_LOST_WARNING, "react_to_post")
                raise
            return result

        except ToolError:
            raise
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "react_to_post")
        except Exception as e:
            raise_tool_error(e, "react_to_post")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Comment On Post",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"engage"},
    )
    async def comment_on_post(
        post_url: str,
        text: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """
        Post a top-level comment on a LinkedIn post. This is a write operation.

        Args:
            post_url: Post permalink, "/feed/update/<urn>/" or "/posts/<slug>",
                as a full URL or a relative path, or the post's urn
            text: Single-line comment text, posted exactly as given. No mention
                or hashtag suggestion is accepted. C0 control characters and
                DEL are rejected, including CR, LF and tab.
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, status and retry_safe.
            Statuses: commented, comments_disabled, post_unavailable,
            comment_failed, outcome_unknown. ``commented`` means the editor
            emptied and the text rendered on the page after Post was pressed
            once. ``comment_failed`` never pressed Post, and a draft already in
            the editor is left untouched. ``retry_safe`` is false once Post was
            pressed; on ``outcome_unknown`` check the post before calling
            again, because a repeat may post the comment twice.
        """
        try:
            post_url = normalize_post_url(post_url)
            reason = invalid_comment_reason(text)
            if reason is not None:
                raise ToolError(reason)
            extractor = await get_ready_extractor(ctx, tool_name="comment_on_post")
            logger.info("Commenting on %s (%d chars)", post_url, len(text))
            await ctx.report_progress(progress=0, total=100, message="Commenting")

            result = await extractor.comment_on_post(post_url, text)

            try:
                await ctx.report_progress(progress=100, total=100, message="Complete")
            except BaseException:
                if result.get("retry_safe") is False:
                    logger.warning(_RESULT_LOST_WARNING, "comment_on_post")
                raise
            return result

        except ToolError:
            raise
        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "comment_on_post")
        except Exception as e:
            raise_tool_error(e, "comment_on_post")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Connection State",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"engage"},
    )
    async def get_connection_state(
        linkedin_username: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """
        Read the connection state with a person without clicking anything.

        Args:
            linkedin_username: LinkedIn username (e.g., "stickerdaniel"). A full
                profile URL is accepted too and is reduced to the username.
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url and state. States: pending, already_connected,
            connectable, follow_only, incoming_request, self_profile,
            unavailable. A profile whose Connect sits only under the More menu
            reads follow_only here; connect_with_person opens that menu and
            may still send an invitation.
        """
        try:
            linkedin_username = normalize_person_identifier(linkedin_username)
            extractor = await get_ready_extractor(ctx, tool_name="get_connection_state")
            logger.info("Reading connection state for %s", linkedin_username)
            await ctx.report_progress(progress=0, total=100, message="Reading profile")

            result = await extractor.get_connection_state(linkedin_username)

            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_connection_state")
        except Exception as e:
            raise_tool_error(e, "get_connection_state")  # NoReturn

    @mcp.tool(
        timeout=tool_timeout,
        title="Get Post Author",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"engage"},
    )
    async def get_post_author(
        post_url: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """
        Read who wrote a LinkedIn post.

        Args:
            post_url: Post permalink, "/feed/update/<urn>/" or "/posts/<slug>",
                as a full URL or a relative path, or the post's urn
            ctx: FastMCP context for progress reporting

        Returns:
            Dict with url, status, name and username. status is ok when an
            author was read and unreadable when the post could not be read,
            in which case name and username are null. username is the
            author's /in/ username, or null for a company post. On a repost
            the original post's author is read, not the member who reposted.
        """
        try:
            post_url = normalize_post_url(post_url)
            extractor = await get_ready_extractor(ctx, tool_name="get_post_author")
            logger.info("Reading the author of %s", post_url)
            await ctx.report_progress(progress=0, total=100, message="Reading post")

            result = await extractor.get_post_author(post_url)

            await ctx.report_progress(progress=100, total=100, message="Complete")
            return result

        except AuthenticationError as e:
            try:
                await handle_auth_error(e, ctx)
            except Exception as relogin_exc:
                raise_tool_error(relogin_exc, "get_post_author")
        except Exception as e:
            raise_tool_error(e, "get_post_author")  # NoReturn
