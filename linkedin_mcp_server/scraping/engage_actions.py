"""Reactions, comments and author reads on one post permalink page.

Per the AGENTS.md Scraping Rules no decision here reads a label value. Each
control is found by structure, by attribute presence or by a value LinkedIn
does not translate:

* a reaction is named by ``data-test-reactions-icon-type`` (``LIKE``,
  ``PRAISE``, ``APPRECIATION``, ``EMPATHY``, ``INTEREST``,
  ``ENTERTAINMENT``), LinkedIn's reaction enum rather than a word. The
  reactions menu is the one group of buttons carrying one icon each and all
  six types.
* the post's react trigger is a ``button[aria-pressed]`` that passes three
  checks, because a header control such as Follow can carry the same
  attribute:

  1. it sits in an action bar: the smallest ancestor holding three or more
     plain buttons (visible, no reaction icon) holds no other
     ``button[aria-pressed]`` outside a reactions menu. Follow's smallest such
     ancestor is the whole post, which also holds the real trigger.
  2. when the requested URL names a post id, the nearest ``[data-urn]``
     ancestor carries that id in an attribute of its own or of an element
     inside it, so the trigger belongs to the requested post.
  3. hovering it opens a reactions menu with all six types. Hovering is not a
     click and changes nothing on LinkedIn.

* the comment editor is the first visible ``[role=textbox][contenteditable]``
  in ``<main>`` outside a dialog, and its Post button is the one
  ``button[type=submit]`` of the form around it.
* the Comment button, needed only when the editor is not open, is the next
  plain button after the validated trigger in its action bar. This one is
  positional; the bar's order (react, comment, repost, send) is the same in
  every locale.
* the author is read from the post's actor block: the first ``/in/`` or
  ``/company/`` link holding two or more ``aria-hidden`` text parts (name and
  headline). A "reposted this" header links its member with plain text, so
  the original author is read rather than the reposter. With no such link,
  the first ``/in/`` or ``/company/`` link is used.

The address is checked after the load, after the post appears, and again inside
every click program, so a page that moves itself to the feed after loading
leaves the feed untouched.

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
import re
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
_EDITOR = 'main [role="textbox"][contenteditable="true"]'

# The post id a permalink names: the urn's number, or the activity number a
# /posts/ slug ends with.
_URN_ID = re.compile(r"urn:li:(?:activity|share|ugcPost):([0-9]+)")
_SLUG_ID = re.compile(r"activity-([0-9]+)")

# Budgets. The page is already loaded when each starts, so they cover LinkedIn
# rendering a control or committing an action, not a navigation.
_LOAD_TIMEOUT_MS = 10_000
_MENU_TIMEOUT = 3.0
_EDITOR_TIMEOUT_MS = 3_000
_SUBMIT_READY_TIMEOUT = 3.0
_CONFIRM_TIMEOUT = 10.0
_POLL = 0.15

# Shared by every program below. Markers the policy traces name programs by
# must stay out of this block.
_HELPERS_JS = r"""
const ICON = '[data-test-reactions-icon-type]';
const TYPES = ['LIKE', 'PRAISE', 'APPRECIATION', 'EMPATHY', 'INTEREST', 'ENTERTAINMENT'];
const visible = el => !!(
  el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length)
);
const norm = s => (s || '').replace(/ /g, ' ').replace(/\s+/g, ' ').trim();
const onPost = () => /^\/(feed\/update|posts)\//.test(location.pathname);
const iconType = el => {
  const icon = el && el.querySelector(ICON);
  return icon ? icon.getAttribute('data-test-reactions-icon-type') : null;
};
const oneIcon = b => b.querySelectorAll(ICON).length === 1;
const menuOf = button => {
  let el = button.parentElement;
  while (el) {
    const group = Array.from(el.querySelectorAll('button')).filter(oneIcon);
    if (group.length >= TYPES.length) {
      const seen = new Set(group.map(iconType));
      const complete = group.length === TYPES.length && TYPES.every(t => seen.has(t));
      return complete ? group : null;
    }
    el = el.parentElement;
  }
  return null;
};
const isMenuItem = b => oneIcon(b) && !!menuOf(b);
const triggers = main => Array.from(main.querySelectorAll('button[aria-pressed]'));
const findBar = (main, trigger) => {
  const plain = b => b === trigger || (visible(b) && !b.querySelector(ICON));
  let el = trigger.parentElement;
  while (el && el !== main.parentElement) {
    const buttons = Array.from(el.querySelectorAll('button')).filter(plain);
    if (buttons.length >= 3) {
      const others = Array.from(el.querySelectorAll('button[aria-pressed]')).filter(
        b => b !== trigger && !isMenuItem(b)
      );
      return others.length === 0 ? buttons : null;
    }
    el = el.parentElement;
  }
  return null;
};
const findEditor = main => {
  for (const el of main.querySelectorAll('[role="textbox"][contenteditable="true"]')) {
    if (visible(el) && !el.closest('dialog, [role="dialog"]')) return el;
  }
  return null;
};
"""

# Indices, among ``main button[aria-pressed]`` in document order, of the
# buttons that sit in an action bar and belong to the requested post.
TRIGGER_CANDIDATES_JS = (
    r"""
((postId) => {
"""
    + _HELPERS_JS
    + r"""
  const main = document.querySelector('main');
  if (!main) return [];
  const ofPost = trigger => {
    if (!postId) return true;
    const holder = trigger.closest('[data-urn]');
    if (!holder) return false;
    for (const el of [holder, ...holder.querySelectorAll('*')]) {
      for (const attr of el.attributes) {
        if (attr.value.includes(postId)) return true;
      }
    }
    return false;
  };
  const candidates = [];
  triggers(main).forEach((b, i) => {
    if (findBar(main, b) && ofPost(b)) candidates.push(i);
  });
  return candidates;
})
"""
)

# Whether a complete reactions menu is showing: a visible button with one icon
# whose smallest six-or-more group is exactly the six types.
MENU_OPEN_JS = (
    r"""
(() => {
"""
    + _HELPERS_JS
    + r"""
  const shown = Array.from(document.querySelectorAll('button')).filter(
    b => visible(b) && oneIcon(b)
  );
  return shown.some(b => !!menuOf(b));
})
"""
)

# The validated trigger's state: pressed, and which reaction type its icon
# names (null when it names none).
POST_STATE_JS = (
    r"""
((index) => {
"""
    + _HELPERS_JS
    + r"""
  const main = document.querySelector('main');
  const trigger = main ? triggers(main)[index] : null;
  if (!trigger) return {hasTrigger: false};
  return {
    hasTrigger: true,
    pressed: trigger.getAttribute('aria-pressed') === 'true',
    currentType: iconType(trigger),
  };
})
"""
)

# Click the requested reaction in the open menu: 'clicked', 'missing' while no
# unique visible menu item of that type exists, or 'moved' when the page has
# left the post. The trigger itself is never a menu item.
CLICK_REACTION_JS = (
    r"""
((arg) => {
"""
    + _HELPERS_JS
    + r"""
  if (!onPost()) return 'moved';
  const main = document.querySelector('main');
  const trigger = main ? triggers(main)[arg.index] : null;
  const matches = Array.from(document.querySelectorAll('button')).filter(
    b => b !== trigger && visible(b) && oneIcon(b) && iconType(b) === arg.type &&
      !!menuOf(b)
  );
  if (matches.length !== 1) return 'missing';
  matches[0].click();
  return 'clicked';
})
"""
)

# Open the comment editor from the validated trigger's action bar: the next
# plain button after the trigger. 'clicked', 'missing' or 'moved'.
CLICK_COMMENT_BUTTON_JS = (
    r"""
((index) => {
"""
    + _HELPERS_JS
    + r"""
  if (!onPost()) return 'moved';
  const main = document.querySelector('main');
  const trigger = main ? triggers(main)[index] : null;
  const buttons = trigger ? findBar(main, trigger) : null;
  if (!buttons) return 'missing';
  const next = buttons[buttons.indexOf(trigger) + 1];
  if (!next) return 'missing';
  next.click();
  return 'clicked';
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

# Remove what this call wrote, used when the editor did not end up holding
# exactly the requested text or the text will not be posted.
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

# The editor's form's one submit button: 'ready', 'disabled', 'missing', or
# 'moved' when the page has left the post. With click=true a ready button is
# clicked and 'clicked' is returned.
SUBMIT_COMMENT_JS = (
    r"""
((click) => {
"""
    + _HELPERS_JS
    + r"""
  if (!onPost()) return 'moved';
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

# The post's author, from its actor block (see the module docstring). The name
# is the first non-empty aria-hidden part of a link to the author, else the
# first line of one.
POST_AUTHOR_JS = (
    r"""
(() => {
"""
    + _HELPERS_JS
    + r"""
  const main = document.querySelector('main');
  if (!main) return null;
  const target = a => {
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
  const parts = a => Array.from(a.querySelectorAll('[aria-hidden="true"]'))
    .map(e => norm(e.textContent))
    .filter(Boolean);
  const links = Array.from(main.querySelectorAll('a[href]'))
    .map(a => ({a, to: target(a)}))
    .filter(link => link.to);
  if (!links.length) return null;
  const actor = links.find(link => parts(link.a).length >= 2) || links[0];
  const same = links.filter(
    link => link.to.kind === actor.to.kind && link.to.slug === actor.to.slug
  );
  let name = '';
  for (const link of same) {
    const text = parts(link.a)[0] || norm((link.a.innerText || '').split('\n')[0]);
    if (text) {
      name = text;
      break;
    }
  }
  return {kind: actor.to.kind, slug: actor.to.slug, name};
})
"""
)


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


def post_id(post_url: str) -> str | None:
    """The numeric post id a normalized permalink names, if it names one."""
    match = _URN_ID.search(post_url) or _SLUG_ID.search(post_url)
    return match.group(1) if match else None


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

    def _on_post(self) -> bool:
        """Whether the page is still at a post permalink.

        A removed or hidden post can land on the feed, or move there after it
        loaded, where the first react trigger belongs to some other post.
        """
        return urlparse(self._session.page.url).path.startswith(_POST_PATH_PREFIXES)

    async def _load_post(self, post_url: str) -> bool:
        """Open the permalink; False when LinkedIn sent us somewhere else."""
        page = self._session.page
        await self._navigator._navigate_to_page(post_url)
        await self._session.check_rate_limit()
        try:
            await page.wait_for_selector("main", timeout=_LOAD_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            logger.debug("No <main> on %s", page.url)
        return self._on_post()

    async def _post_is_shown(self) -> bool:
        try:
            await self._session.page.wait_for_selector(
                _REACT_TRIGGER, timeout=_LOAD_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            return False
        return self._on_post()

    async def _menu_opens(self) -> bool:
        deadline = self._session.monotonic() + _MENU_TIMEOUT
        while True:
            if await self._session.page.evaluate(MENU_OPEN_JS):
                return True
            if self._session.monotonic() >= deadline:
                return False
            await asyncio.sleep(_POLL)

    async def _find_trigger(self, post_url: str) -> int | None:
        """The index of the post's react trigger, validated by its menu.

        Each candidate is hovered in document order and the first one that
        opens a complete reactions menu is the trigger. None when no
        candidate does, or when the page leaves the post meanwhile.
        """
        page = self._session.page
        candidates = await page.evaluate(TRIGGER_CANDIDATES_JS, post_id(post_url))
        if not isinstance(candidates, list):
            return None
        for index in candidates:
            if not isinstance(index, int) or not self._on_post():
                return None
            await page.locator(_REACT_TRIGGER).nth(index).hover(timeout=5000)
            if await self._menu_opens():
                return index if self._on_post() else None
        return None

    async def _post_state(self, index: int) -> dict[str, Any]:
        state = await self._session.page.evaluate(POST_STATE_JS, index)
        return state if isinstance(state, dict) else {"hasTrigger": False}

    async def _pick_reaction(self, index: int, reaction_type: str) -> str:
        """Click the reaction once its menu item renders: 'clicked', 'missing'
        or 'moved'."""
        deadline = self._session.monotonic() + _MENU_TIMEOUT
        argument = {"index": index, "type": reaction_type}
        while True:
            outcome = await self._session.page.evaluate(CLICK_REACTION_JS, argument)
            if outcome in ("clicked", "moved"):
                return outcome
            if self._session.monotonic() >= deadline:
                return "missing"
            await asyncio.sleep(_POLL)

    async def _reaction_confirmed(self, index: int, reaction_type: str) -> bool:
        """Wait for the trigger to show exactly the requested reaction.

        A pressed trigger naming no type proves nothing about which reaction
        landed, so it never confirms.
        """
        deadline = self._session.monotonic() + _CONFIRM_TIMEOUT
        while True:
            state = await self._post_state(index)
            if state.get("pressed") and state.get("currentType") == reaction_type:
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

        def unavailable() -> dict[str, Any]:
            return _react_result(
                post_url, "post_unavailable", reaction, retry_safe=True
            )

        if not await self._load_post(post_url) or not await self._post_is_shown():
            return unavailable()
        index = await self._find_trigger(post_url)
        if index is None:
            return unavailable()

        state = await self._post_state(index)
        if not state.get("hasTrigger"):
            return unavailable()
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
        may_have_reacted = False
        try:
            if not self._on_post():
                return unavailable()
            if use_trigger:
                may_have_reacted = True
                await (
                    self._session.page.locator(_REACT_TRIGGER)
                    .nth(index)
                    .click(timeout=5000)
                )
            else:
                may_have_reacted = True
                picked = await self._pick_reaction(index, reaction_type)
                if picked != "clicked":
                    may_have_reacted = False
                    if picked == "moved":
                        return unavailable()
                    return _react_result(
                        post_url, "react_failed", reaction, retry_safe=True
                    )

            if await self._reaction_confirmed(index, reaction_type):
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

    async def _open_editor(self, index: int) -> str:
        """'open', 'disabled' or 'moved'."""
        page = self._session.page
        if await page.locator(f"{_EDITOR} >> visible=true").count() > 0:
            return "open"
        if not self._on_post():
            return "moved"
        clicked = await page.evaluate(CLICK_COMMENT_BUTTON_JS, index)
        if clicked == "moved":
            return "moved"
        if clicked != "clicked":
            return "disabled"
        try:
            await page.wait_for_selector(
                _EDITOR, state="visible", timeout=_EDITOR_TIMEOUT_MS
            )
        except PlaywrightTimeoutError:
            return "disabled"
        return "open"

    async def _submit_ready(self) -> str:
        """'ready', 'moved', or anything else for not usable."""
        deadline = self._session.monotonic() + _SUBMIT_READY_TIMEOUT
        while True:
            state = await self._session.page.evaluate(SUBMIT_COMMENT_JS, False)
            if state != "disabled" or self._session.monotonic() >= deadline:
                return str(state)
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

        def unavailable() -> dict[str, Any]:
            return _comment_result(post_url, "post_unavailable", retry_safe=True)

        if not await self._load_post(post_url) or not await self._post_is_shown():
            return unavailable()
        index = await self._find_trigger(post_url)
        if index is None:
            return unavailable()
        opened = await self._open_editor(index)
        if opened == "moved":
            return unavailable()
        if opened != "open":
            return _comment_result(post_url, "comments_disabled", retry_safe=True)

        page = self._session.page
        written = await page.evaluate(WRITE_COMMENT_JS, text)
        if written != "written":
            if written == "mismatch":
                await self._clear_editor()
            logger.info("Comment editor refused the text: %s", written)
            return _comment_result(post_url, "comment_failed", retry_safe=True)

        ready = "moved" if not self._on_post() else await self._submit_ready()
        if ready != "ready":
            await self._clear_editor()
            if ready == "moved":
                return unavailable()
            return _comment_result(post_url, "comment_failed", retry_safe=True)

        before = await page.evaluate(COMMENT_STATE_JS, text)
        shown_before = int(before.get("shown") or 0) if isinstance(before, dict) else 0

        may_have_posted = False
        try:
            if not self._on_post():
                await self._clear_editor()
                return unavailable()
            may_have_posted = True
            submitted = await page.evaluate(SUBMIT_COMMENT_JS, True)
            if submitted != "clicked":
                may_have_posted = False
                await self._clear_editor()
                if submitted == "moved":
                    return unavailable()
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
        """The post author's name and /in/ username (null for a company page).

        ``status`` is ``ok`` when an author link was read and ``unreadable``
        when the page held none or was not the post.
        """
        result: dict[str, Any] = {
            "url": post_url,
            "status": "unreadable",
            "name": None,
            "username": None,
        }
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
        if not self._on_post():
            return result

        author = await self._session.page.evaluate(POST_AUTHOR_JS)
        if not isinstance(author, dict):
            return result
        result["status"] = "ok"
        result["name"] = author.get("name") or None
        if author.get("kind") == "in":
            try:
                result["username"] = normalize_person_identifier(
                    str(author.get("slug") or "")
                )
            except InvalidReferenceError:
                logger.debug("Author link carried no usable username: %r", author)
        return result
