"""Reactions, comments and author reads on one post permalink page.

Per the AGENTS.md Scraping Rules no decision here reads a label value. Each
control is found by structure, by attribute presence or by a value LinkedIn
does not translate:

* the post's react trigger is the first ``button[aria-pressed]`` in
  ``<main>``. The post's own action bar renders before its comments, and each
  comment's react button carries the same attribute, so document order picks
  the post's one.
* a reaction is named by ``data-test-reactions-icon-type`` (``LIKE``,
  ``PRAISE``, ``APPRECIATION``, ``EMPATHY``, ``INTEREST``,
  ``ENTERTAINMENT``), LinkedIn's reaction enum rather than a word. The menu is
  the one group of buttons carrying one icon each and all six types; the
  social-counts button carries icons too, and never all six as separate
  buttons.
* the comment editor is the first visible ``[role=textbox][contenteditable]``
  in ``<main>`` outside a dialog, and its Post button is the one
  ``button[type=submit]`` of the form around it.
* the Comment button, needed only when the editor is not open, is the next
  visible button after the react trigger in the action bar. This one is
  positional; the bar's order (react, comment, repost, send) is the same in
  every locale.
* the author is the first ``/in/`` or ``/company/`` link in ``<main>``.

Text goes in through ``execCommand('insertText')`` and clicks are DOM
``click()`` calls, as the message composer does it. No key event reaches the
editor, so no mention or hashtag suggestion can be accepted.

``retry_safe`` follows ``contracts.message_action_result``: false from the
moment a click that may apply the action is dispatched, true only while
nothing can have changed on LinkedIn.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import InvalidReferenceError
from linkedin_mcp_server.scraping.identifiers import normalize_person_identifier
from linkedin_mcp_server.scraping.navigation import PageNavigator
from linkedin_mcp_server.scraping.session import ScrapingSession

logger = logging.getLogger(__name__)

#: Reaction name a caller passes, to the reaction type LinkedIn renders.
REACTION_TYPES: dict[str, str] = {
    "like": "LIKE",
    "celebrate": "PRAISE",
    "support": "APPRECIATION",
    "love": "EMPATHY",
    "insightful": "INTEREST",
    "funny": "ENTERTAINMENT",
}
_REACTION_NAMES = {value: key for key, value in REACTION_TYPES.items()}

_POST_PATH_PREFIXES = ("/feed/update/", "/posts/")
_REACT_TRIGGER = "main button[aria-pressed]"

# Budgets. The page is already loaded when each starts, so they cover LinkedIn
# rendering a control or committing an action, not a navigation.
_LOAD_TIMEOUT_MS = 10_000
_MENU_TIMEOUT = 3.0
_EDITOR_TIMEOUT_MS = 3_000
_SUBMIT_READY_TIMEOUT = 3.0
_CONFIRM_TIMEOUT = 10.0
_POLL = 0.15

# Shared by every program below.
_HELPERS_JS = r"""
const ICON = '[data-test-reactions-icon-type]';
const TYPES = ['LIKE', 'PRAISE', 'APPRECIATION', 'EMPATHY', 'INTEREST', 'ENTERTAINMENT'];
const visible = el => !!(
  el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length)
);
const norm = s => (s || '').replace(/ /g, ' ').replace(/\s+/g, ' ').trim();
const reactTrigger = main => main.querySelector('button[aria-pressed]');
const iconType = el => {
  const icon = el && el.querySelector(ICON);
  return icon ? icon.getAttribute('data-test-reactions-icon-type') : null;
};
const findEditor = main => {
  for (const el of main.querySelectorAll('[role="textbox"][contenteditable="true"]')) {
    if (visible(el) && !el.closest('dialog, [role="dialog"]')) return el;
  }
  return null;
};
"""

# The post's reaction state: whether a trigger exists, whether it is pressed,
# and which reaction type its icon names (null when it names none).
POST_STATE_JS = (
    r"""
(() => {
"""
    + _HELPERS_JS
    + r"""
  const main = document.querySelector('main');
  if (!main) return {hasTrigger: false};
  const trigger = reactTrigger(main);
  if (!trigger) return {hasTrigger: false};
  return {
    hasTrigger: true,
    pressed: trigger.getAttribute('aria-pressed') === 'true',
    currentType: iconType(trigger),
  };
})
"""
)

# Click the requested reaction in the open menu. The menu is recognised by its
# shape: a button carrying exactly one icon of the requested type whose
# smallest ancestor holding six or more such buttons holds exactly six, one of
# each type. The counts button fails that (its ancestor also holds the menu,
# so seven), and a hidden menu is not visible yet. A unique match is required;
# anything else clicks nothing.
CLICK_REACTION_JS = (
    r"""
((type) => {
"""
    + _HELPERS_JS
    + r"""
  const main = document.querySelector('main');
  const trigger = main ? reactTrigger(main) : null;
  const oneIcon = b => b !== trigger && visible(b) && b.querySelectorAll(ICON).length === 1;
  const matches = [];
  for (const button of document.querySelectorAll('button')) {
    if (!oneIcon(button) || iconType(button) !== type) continue;
    let el = button.parentElement;
    while (el) {
      const group = Array.from(el.querySelectorAll('button')).filter(oneIcon);
      if (group.length >= TYPES.length) {
        const seen = new Set(group.map(iconType));
        if (group.length === TYPES.length && TYPES.every(t => seen.has(t))) {
          matches.push(button);
        }
        break;
      }
      el = el.parentElement;
    }
  }
  if (matches.length !== 1) return false;
  matches[0].click();
  return true;
})
"""
)

# Open the comment editor from the action bar: the next visible button after
# the react trigger inside the smallest ancestor holding three or more of them.
# Menu and counts buttons carry reaction icons and are not bar buttons.
CLICK_COMMENT_BUTTON_JS = (
    r"""
(() => {
"""
    + _HELPERS_JS
    + r"""
  const main = document.querySelector('main');
  const trigger = main ? reactTrigger(main) : null;
  if (!trigger) return false;
  const barButton = b => b === trigger || (visible(b) && !b.querySelector(ICON));
  let el = trigger.parentElement;
  while (el && el !== main.parentElement) {
    const buttons = Array.from(el.querySelectorAll('button')).filter(barButton);
    if (buttons.length >= 3) {
      const next = buttons[buttons.indexOf(trigger) + 1];
      if (!next) return false;
      next.click();
      return true;
    }
    el = el.parentElement;
  }
  return false;
})
"""
)

# Put the comment into the empty editor. Text already there belongs to whoever
# typed it, so an occupied editor is left alone.
WRITE_COMMENT_JS = (
    r"""
((text) => {
"""
    + _HELPERS_JS
    + r"""
  const main = document.querySelector('main');
  const editor = main ? findEditor(main) : null;
  if (!editor) return 'missing';
  if (norm(editor.textContent)) return 'occupied';
  if (
    typeof document.execCommand !== 'function' ||
    !document.queryCommandSupported('insertText')
  ) {
    return 'unsupported';
  }
  editor.focus();
  const range = document.createRange();
  range.selectNodeContents(editor);
  range.collapse(false);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  if (!document.execCommand('insertText', false, text)) return 'unsupported';
  return norm(editor.innerText) === norm(text) ? 'written' : 'mismatch';
})
"""
)

# Remove what this call wrote, used only when the editor did not end up
# holding exactly the requested text.
CLEAR_COMMENT_JS = (
    r"""
(() => {
"""
    + _HELPERS_JS
    + r"""
  const main = document.querySelector('main');
  const editor = main ? findEditor(main) : null;
  if (!editor) return false;
  editor.focus();
  const range = document.createRange();
  range.selectNodeContents(editor);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  document.execCommand('delete', false);
  return !norm(editor.textContent);
})
"""
)

# The editor's form's one submit button: 'ready', 'disabled' or 'missing'.
# With click=true a ready button is clicked and 'clicked' is returned.
SUBMIT_COMMENT_JS = (
    r"""
((click) => {
"""
    + _HELPERS_JS
    + r"""
  const main = document.querySelector('main');
  const editor = main ? findEditor(main) : null;
  const form = editor ? editor.closest('form') : null;
  if (!form) return 'missing';
  const submits = Array.from(form.querySelectorAll('button[type="submit"]')).filter(visible);
  if (submits.length !== 1) return 'missing';
  const submit = submits[0];
  if (submit.disabled || submit.getAttribute('aria-disabled') === 'true') {
    return 'disabled';
  }
  if (!click) return 'ready';
  submit.click();
  return 'clicked';
})
"""
)

# How many rendered places outside any editor show the comment: elements whose
# text contains it while none of their children does. Matched on the first 60
# characters, so a comment LinkedIn shortens behind "see more" still counts.
COMMENT_STATE_JS = (
    r"""
((text) => {
"""
    + _HELPERS_JS
    + r"""
  const main = document.querySelector('main');
  if (!main) return {shown: 0, editorEmpty: false};
  const needle = norm(text).slice(0, 60);
  let shown = 0;
  for (const el of main.querySelectorAll('*')) {
    if (el.closest('[contenteditable="true"]')) continue;
    if (!norm(el.textContent).includes(needle)) continue;
    const inner = Array.from(el.children).some(
      child => norm(child.textContent).includes(needle)
    );
    if (!inner) shown += 1;
  }
  const editor = findEditor(main);
  return {shown, editorEmpty: !editor || !norm(editor.textContent)};
})
"""
)

# The post's author: the first /in/ or /company/ link in <main>. The name is
# the first non-empty aria-hidden span (the visible copy beside a visually
# hidden duplicate), else the first line of any link to the same page.
POST_AUTHOR_JS = r"""
(() => {
  const main = document.querySelector('main');
  if (!main) return null;
  const route = a => {
    let url;
    try {
      url = new URL(a.getAttribute('href'), 'https://www.linkedin.com/');
    } catch {
      return null;
    }
    if (!/(^|\.)linkedin\.com$/.test(url.hostname)) return null;
    const m = url.pathname.match(/^\/(in|company)\/([^\/]+)/);
    return m ? {kind: m[1], slug: m[2]} : null;
  };
  const anchors = Array.from(main.querySelectorAll('a[href]'));
  for (const a of anchors) {
    const found = route(a);
    if (!found) continue;
    let name = '';
    for (const b of anchors) {
      const other = route(b);
      if (!other || other.kind !== found.kind || other.slug !== found.slug) continue;
      const hidden = b.querySelector('[aria-hidden="true"]');
      const text = hidden ? hidden.textContent : (b.innerText || '').split('\n')[0];
      if ((text || '').trim()) {
        name = text.trim();
        break;
      }
    }
    return {kind: found.kind, slug: found.slug, name};
  }
  return null;
})
"""


def invalid_comment_reason(text: str) -> str | None:
    """Why a comment cannot be posted as given, or None when it can.

    The same boundary ``send_message`` draws: blank text and control
    characters, line breaks included, are refused, because the editor
    insertion path is for plain single-line text.
    """
    if not text.strip():
        return "Comment must contain non-whitespace characters."
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        return "Comment must not contain control characters or line breaks."
    return None


def _react_result(
    url: str, status: str, reaction: str | None, *, retry_safe: bool
) -> dict[str, Any]:
    return {
        "url": url,
        "status": status,
        "reaction": reaction,
        "retry_safe": retry_safe,
    }


def _comment_result(url: str, status: str, *, retry_safe: bool) -> dict[str, Any]:
    return {"url": url, "status": status, "retry_safe": retry_safe}


class EngageActions:
    """React to, comment on and read the author of one LinkedIn post."""

    def __init__(self, session: ScrapingSession, navigator: PageNavigator):
        self._session = session
        self._navigator = navigator

    async def _load_post(self, post_url: str) -> bool:
        """Open the permalink; False when LinkedIn sent us somewhere else.

        A removed or hidden post can land on the feed, where the first react
        trigger belongs to some other post. Checking the route before any
        control is looked for keeps that post untouched.
        """
        page = self._session.page
        await self._navigator._navigate_to_page(post_url)
        await self._session.check_rate_limit()
        try:
            await page.wait_for_selector("main", timeout=_LOAD_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            logger.debug("No <main> on %s", page.url)
        return urlparse(page.url).path.startswith(_POST_PATH_PREFIXES)

    async def _post_is_shown(self) -> bool:
        try:
            await self._session.page.wait_for_selector(
                _REACT_TRIGGER, timeout=_LOAD_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            return False
        return True

    async def _post_state(self) -> dict[str, Any]:
        state = await self._session.page.evaluate(POST_STATE_JS)
        return state if isinstance(state, dict) else {"hasTrigger": False}

    async def _pick_reaction(self, reaction_type: str) -> bool:
        """Click the reaction in the menu once it renders; False if it never does."""
        deadline = self._session.monotonic() + _MENU_TIMEOUT
        while True:
            if await self._session.page.evaluate(CLICK_REACTION_JS, reaction_type):
                return True
            if self._session.monotonic() >= deadline:
                return False
            await asyncio.sleep(_POLL)

    async def _reaction_confirmed(self, reaction_type: str) -> bool:
        """Wait for the trigger to show the reaction as applied."""
        deadline = self._session.monotonic() + _CONFIRM_TIMEOUT
        while True:
            state = await self._post_state()
            current = state.get("currentType")
            if state.get("pressed") and current in (None, reaction_type):
                return True
            if self._session.monotonic() >= deadline:
                return False
            await asyncio.sleep(_POLL)

    async def react_to_post(self, post_url: str, reaction: str) -> dict[str, Any]:
        """Apply one reaction to the post, switching from another one if needed.

        ``already_reacted`` means the requested reaction was in place and
        nothing was clicked. A pressed trigger whose icon names no reaction
        type is reported the same way with ``reaction`` null, since neither a
        switch nor a repeat can be judged safe then.
        """
        reaction_type = REACTION_TYPES.get(reaction)
        if reaction_type is None:
            raise ValueError(f"Unknown reaction: {reaction!r}")

        if not await self._load_post(post_url) or not await self._post_is_shown():
            return _react_result(
                post_url, "post_unavailable", reaction, retry_safe=True
            )

        state = await self._post_state()
        if not state.get("hasTrigger"):
            return _react_result(
                post_url, "post_unavailable", reaction, retry_safe=True
            )
        current = state.get("currentType")
        if state.get("pressed"):
            if current == reaction_type:
                return _react_result(
                    post_url, "already_reacted", reaction, retry_safe=True
                )
            if current not in _REACTION_NAMES:
                return _react_result(post_url, "already_reacted", None, retry_safe=True)

        # Only an unpressed trigger may be clicked for like: on a pressed one
        # the same click takes the reaction away.
        use_trigger = reaction_type == "LIKE" and not state.get("pressed")
        trigger = self._session.page.locator(_REACT_TRIGGER).first
        may_have_reacted = False
        try:
            if use_trigger:
                may_have_reacted = True
                await trigger.click(timeout=5000)
            else:
                await trigger.hover(timeout=5000)
                may_have_reacted = True
                if not await self._pick_reaction(reaction_type):
                    may_have_reacted = False
                    return _react_result(
                        post_url, "react_failed", reaction, retry_safe=True
                    )

            if await self._reaction_confirmed(reaction_type):
                return _react_result(post_url, "reacted", reaction, retry_safe=False)
            return _react_result(
                post_url, "outcome_unknown", reaction, retry_safe=False
            )
        except Exception:
            if not may_have_reacted:
                raise
            logger.debug("Reaction failed after a possible click", exc_info=True)
            return _react_result(
                post_url, "outcome_unknown", reaction, retry_safe=False
            )
        except BaseException:
            if may_have_reacted:
                logger.warning(
                    "A reaction was interrupted after its click; check the post "
                    "before reacting again."
                )
            raise

    async def _open_editor(self) -> bool:
        page = self._session.page
        editor = 'main [role="textbox"][contenteditable="true"]'
        if await page.locator(f"{editor} >> visible=true").count() > 0:
            return True
        if not await page.evaluate(CLICK_COMMENT_BUTTON_JS):
            return False
        try:
            await page.wait_for_selector(
                editor, state="visible", timeout=_EDITOR_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            return False
        return True

    async def _submit_ready(self) -> bool:
        deadline = self._session.monotonic() + _SUBMIT_READY_TIMEOUT
        while True:
            state = await self._session.page.evaluate(SUBMIT_COMMENT_JS, False)
            if state == "ready":
                return True
            if state != "disabled" or self._session.monotonic() >= deadline:
                return False
            await asyncio.sleep(_POLL)

    async def _comment_confirmed(self, text: str, shown_before: int) -> bool:
        """Wait for the editor to empty and the comment to render once more."""
        deadline = self._session.monotonic() + _CONFIRM_TIMEOUT
        while True:
            state = await self._session.page.evaluate(COMMENT_STATE_JS, text)
            if (
                isinstance(state, dict)
                and state.get("editorEmpty")
                and int(state.get("shown") or 0) > shown_before
            ):
                return True
            if self._session.monotonic() >= deadline:
                return False
            await asyncio.sleep(_POLL)

    async def _clear_editor(self) -> None:
        try:
            await self._session.page.evaluate(CLEAR_COMMENT_JS)
        except Exception:
            logger.debug("Could not clear the comment editor", exc_info=True)

    async def comment_on_post(self, post_url: str, text: str) -> dict[str, Any]:
        """Post one top-level comment, exactly as given, and confirm it rendered."""
        if invalid_comment_reason(text) is not None:
            return _comment_result(post_url, "comment_failed", retry_safe=True)

        if not await self._load_post(post_url) or not await self._post_is_shown():
            return _comment_result(post_url, "post_unavailable", retry_safe=True)
        if not await self._open_editor():
            return _comment_result(post_url, "comments_disabled", retry_safe=True)

        page = self._session.page
        written = await page.evaluate(WRITE_COMMENT_JS, text)
        if written != "written":
            if written == "mismatch":
                await self._clear_editor()
            logger.info("Comment editor refused the text: %s", written)
            return _comment_result(post_url, "comment_failed", retry_safe=True)

        if not await self._submit_ready():
            await self._clear_editor()
            return _comment_result(post_url, "comment_failed", retry_safe=True)

        before = await page.evaluate(COMMENT_STATE_JS, text)
        shown_before = int(before.get("shown") or 0) if isinstance(before, dict) else 0

        may_have_posted = False
        try:
            may_have_posted = True
            if await page.evaluate(SUBMIT_COMMENT_JS, True) != "clicked":
                may_have_posted = False
                await self._clear_editor()
                return _comment_result(post_url, "comment_failed", retry_safe=True)
            if await self._comment_confirmed(text, shown_before):
                return _comment_result(post_url, "commented", retry_safe=False)
            return _comment_result(post_url, "outcome_unknown", retry_safe=False)
        except Exception:
            if not may_have_posted:
                raise
            logger.debug("Comment failed after a possible submit", exc_info=True)
            return _comment_result(post_url, "outcome_unknown", retry_safe=False)
        except BaseException:
            if may_have_posted:
                logger.warning(
                    "A comment was interrupted after its submit; check the post "
                    "before commenting again, as a repeat may post it twice."
                )
            raise

    async def get_post_author(self, post_url: str) -> dict[str, Any]:
        """The post author's name and /in/ username (null for a company page)."""
        result: dict[str, Any] = {"url": post_url, "name": None, "username": None}
        if not await self._load_post(post_url):
            return result
        try:
            # Attached, not visible: the first author link is usually the
            # avatar, which has no size until its image loads, and only the
            # link's address and text are read.
            await self._session.page.wait_for_selector(
                'main a[href*="/in/"], main a[href*="/company/"]',
                state="attached",
                timeout=_LOAD_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            return result

        author = await self._session.page.evaluate(POST_AUTHOR_JS)
        if not isinstance(author, dict):
            return result
        result["name"] = author.get("name") or None
        if author.get("kind") == "in":
            try:
                result["username"] = normalize_person_identifier(
                    str(author.get("slug") or "")
                )
            except InvalidReferenceError:
                logger.debug("Author link carried no usable username: %r", author)
        return result
