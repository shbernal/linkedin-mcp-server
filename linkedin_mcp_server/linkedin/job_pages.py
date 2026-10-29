"""Page-level reads behind the job search and saved-job list workflows.

A lower-level service than the workflows in `jobs.py` rather than a peer of
them: it navigates, scrolls, extracts and counts one page at a time, and
answers with a `JobPageCapture`. The pagination policy, the budgets and the
diagnostics that decide what a page *means* stay with the workflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import asyncio
import logging
import re
import time

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from linkedin_mcp_server.core.exceptions import LinkedInOperationError
from linkedin_mcp_server.core.utils import (
    _JOB_CARD_SELECTOR,
    _RAIL_PICK_JS,
    detect_rate_limit,
    handle_modal_close,
    scroll_job_sidebar,
    scroll_to_bottom,
)
from linkedin_mcp_server.error_diagnostics import build_issue_diagnostics
from linkedin_mcp_server.linkedin.capture import RATE_LIMIT_RETRY_DELAY
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.contracts import (
    RATE_LIMITED_SECTION_TEXT,
    ExtractedSection,
)
from linkedin_mcp_server.linkedin.job_policy import (
    SAFETY_REDIRECT_PATH,
    SAVED_JOBS_PAGE_SIZE,
    SCROLL_DEADLINE_MAX,
    ApplyType,
    JobsTrackerStage,
    employer_apply_url,
    on_tracker_stage,
    route,
    same_job_search,
    tracker_stage_url,
)
from linkedin_mcp_server.linkedin.link_metadata import build_references
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.linkedin.text import (
    JobApplyTextTable,
    filter_linkedin_noise_lines,
    truncate_linkedin_noise,
)

logger = logging.getLogger(__name__)

# What the rate-limit retry has to have left after its backoff before it is
# worth starting. The retry is a whole page again, and a search page measured
# 6.5s on average (ten navigations of a Paris developer search, 83s). One
# admitted with less times out in navigation, which trades the rate-limit
# answer the first attempt already had for a timeout that explains nothing.
_RETRY_PAGE_ALLOWANCE = 6.5


# The id is the trailing run of digits, and LinkedIn serves the same job under
# both `/jobs/view/1967281839/` and `/jobs/view/<title>-at-<company>-1967281839/`.
# Anchoring the digits to the front of the segment loses the slugged form
# entirely, and reads `2026` out of a title that opens with a year.
#
# The slug is anything but a separator, not `[\w-]`: JS `\w` is ASCII, and a
# localized title reaches this as `d%C3%A9veloppeur-web-at-koul-3510216552`,
# where the `%` ends the match and the id is lost. Measured on the guest
# search API for `developpeur`, where 6 of 10 hrefs were percent-encoded.
# The authenticated pages this server visits serve bare ids today (measured
# across job search, collections and a French search: 0 slugs in 27 anchors),
# so this branch is defensive on both counts.
# `scoped` runs the sidebar's own rule again and reads only the container it
# names, because everything outside it is not a search result: the detail pane
# holds its own permalink and, once opened, a similar-jobs module, and counting
# those as rendered results advances the offset past results the rail never
# showed. Re-run rather than remembered, so a rail replaced between the scroll
# and this call is followed instead of silently widening the scope back to the
# document. `get_saved_jobs` reads the document, having no sidebar to scroll
# and no second list to be confused with.
JOB_IDS_JS = (
    r"""(opts) => {
    const {selector, scoped} = opts;
"""
    + _RAIL_PICK_JS
    + r"""
    const picked = scoped ? pickRail() : null;
    const scope = picked || document;
    const cards = scope.querySelectorAll(selector);
    const seen = new Set();
    const ids = [];
    for (const card of cards) {
        // `idOf` from the rail rule above, rather than a second copy of the
        // pattern: the rail is picked by counting ids, so a card shape one
        // side understands and the other does not would have extraction read
        // a container the pick never considered.
        const id = idOf(card);
        if (id && !seen.has(id)) {
            seen.add(id);
            ids.push(id);
        }
    }
    return {ids: ids, scoped: Boolean(picked)};
}"""
)

# The same scope as `JOB_IDS_JS`, since a pane job is not a result either. A
# card is the largest element around a job link that holds no other job, found
# by counting ids and not by class, so the classic `<li>` and the redesigned
# card are both found. Only the label is text, and it comes from the locale
# table.
PROMOTED_JOB_IDS_JS = (
    r"""(opts) => {
    const {selector, label} = opts;
"""
    + _RAIL_PICK_JS
    + r"""
    const scope = pickRail() || document;
    const cardOf = (node) => {
        let card = node;
        while (card.parentElement && card.parentElement !== scope
               && card.parentElement !== document.body
               && idsIn(card.parentElement) === 1) {
            card = card.parentElement;
        }
        return card;
    };
    const seen = new Set();
    const promoted = [];
    for (const node of scope.querySelectorAll(selector)) {
        const id = idOf(node);
        if (!id || seen.has(id)) continue;
        seen.add(id);
        const lines = (cardOf(node).innerText || '').split('\n')
            .map((line) => line.trim());
        if (lines.includes(label)) promoted.push(id);
    }
    return promoted;
}"""
)

# How many unpromoted results sit above the notice the redesigned search puts
# where its matches end, or null when nothing on the page reads as the notice.
# Null rather than zero, because zero is what calls the search a no-match. The
# notice is looked for in the whole document, the cards only in the rail,
# each read as `PROMOTED_JOB_IDS_JS` reads it.
#
# The notice is the smallest element whose text is the whole sentence, reached
# from a text node that opens it, so a sentence LinkedIn splits across inline
# elements is still found. Its text only matched when it was one text node,
# and a page whose notice was split then kept its unrelated postings.
ORGANIC_BEFORE_NOTICE_JS = (
    r"""(opts) => {
    const {selector, label, notice} = opts;
"""
    + _RAIL_PICK_JS
    + r"""
    const scope = pickRail() || document;
    const cardOf = (node) => {
        let card = node;
        while (card.parentElement && card.parentElement !== scope
               && card.parentElement !== document.body
               && idsIn(card.parentElement) === 1) {
            card = card.parentElement;
        }
        return card;
    };
    const isPromoted = (node) => (cardOf(node).innerText || '').split('\n')
        .map((line) => line.trim()).includes(label);
    const norm = (text) => (text || '').replace(/\s+/g, ' ').trim();
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let mark = null;
    while (!mark && walker.nextNode()) {
        const piece = norm(walker.currentNode.data);
        if (!piece || !notice.startsWith(piece)) continue;
        let node = walker.currentNode;
        while (node && norm(node.textContent).length < notice.length) {
            node = node.parentNode;
        }
        if (node && norm(node.textContent) === notice) mark = node;
    }
    if (!mark) return null;
    const seen = new Set();
    let organic = 0;
    for (const node of scope.querySelectorAll(selector)) {
        const id = idOf(node);
        if (!id || seen.has(id)) continue;
        seen.add(id);
        const above = mark.compareDocumentPosition(node)
            & Node.DOCUMENT_POSITION_PRECEDING;
        if (above && !isPromoted(node)) organic += 1;
    }
    return organic;
}"""
)

# This posting's apply control and state, once they render. Easy Apply comes in
# two shapes. The anchor into the posting's own `/apply/` route
# (`<a href=".../jobs/view/<id>/apply/?openSDUIApplyFlow=true">`, measured on
# 2026-09-14) is found by its URL, which the "More jobs" cards, each linking its
# own posting, cannot match. The `<button>` with no href that replaced it
# (measured on 2026-09-25) is found by its text, above the description only; the
# cards below show the same words, as text inside their links.
#
# The external control is an `<a target="_blank">` into the off-site
# interstitial, which names the employer's page in its href (measured on
# 2026-09-19), so it is answered without a click. It and the state lines are
# read above the description heading only: below it sit the "More jobs" cards,
# each with an Apply of its own. Without the heading nothing is proven to be
# this posting's, and the read answers `unknown`.
#
# On 2026-09-14 the external control was a `<button>` with no href, whose click
# opened a "Share your profile?" dialog or a tab. LinkedIn counts that click as
# an apply on the posting, and no posting has shown the button since, so it is
# not read: a posting carrying it answers `unknown` and is never clicked.
#
# The heading is found by walking text nodes rather than asking every element
# for its text. Both find it; the walk visits fewer nodes and copies none of
# them, and `textContent` on every element of a posting copies that posting
# once per level of nesting. That is worth the difference because the readiness
# poll runs this program on every frame for up to ten seconds.
APPLY_SIGNALS_JS = r"""(opts) => {
    const {
        applyPath, redirectPath, easyApplyLabel, externalLabel, descriptionHeadings,
        closedLines, appliedPattern,
    } = opts;
    const main = document.querySelector('main');
    if (!main) return null;
    const pathOf = (anchor) => {
        try {
            return new URL(anchor.href).pathname.replace(/\/+$/, '');
        } catch (error) {
            return '';
        }
    };
    const walk = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
    let heading = null;
    while (walk.nextNode()) {
        if (descriptionHeadings.includes((walk.currentNode.nodeValue || '').trim())) {
            heading = walk.currentNode;
            break;
        }
    }
    const above = (el) => Boolean(
        heading && (heading.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_PRECEDING)
    );
    const lines = (main.innerText || '').split('\n').map((line) => line.trim());
    const end = lines.findIndex((line) => descriptionHeadings.includes(line));
    const top = end === -1 ? [] : lines.slice(0, end);
    const applied = new RegExp(appliedPattern);
    const anchors = [...main.querySelectorAll('a[href]')];
    const link = anchors.find((anchor) => above(anchor)
        && (anchor.innerText || '').trim() === externalLabel
        && pathOf(anchor) === redirectPath);
    return {
        bounded: Boolean(heading),
        easy_apply: anchors.some((anchor) => pathOf(anchor) === applyPath)
            || [...main.querySelectorAll('button')].some((el) => above(el)
                && (el.innerText || '').trim() === easyApplyLabel),
        external_link: link ? link.href : null,
        applied: top.some((line) => applied.test(line)),
        closed: top.some((line) => closedLines.includes(line)),
    };
}"""

# Ready once the description heading and a signal are both in. The Easy Apply
# anchor is found without the heading (the button is not), but the applied and
# closed lines are not, so a read that settled on the anchor alone could call
# such a posting open.
APPLY_READY_JS = (
    "(opts) => {\n    const signals = (" + APPLY_SIGNALS_JS + ")(opts);\n"
    "    return Boolean(signals && signals.bounded && (signals.easy_apply\n"
    "        || signals.external_link || signals.applied || signals.closed));\n}"
)

# How long the apply control gets to render.
_APPLY_READY_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class JobApplyRead:
    """How one posting takes applications, and where the employer's form is."""

    type: ApplyType
    url: str | None = None


# A tracker card is found by structure and never by text: the job's own
# overflow menu is the one visible `button[aria-expanded][aria-label]` whose
# nearest ancestor holding a job link holds that job's links and no other
# job's. Attribute presence only, since the label's words are locale-bound.
# Measured on 2026-09-21: every card carries two such buttons, one of them in
# a collapsed layout with no size, and the tabs' own dropdowns are `div`s.
# More than one match is refused rather than guessed at, so a re-render that
# doubles a card cannot move the click onto a neighbour.
TRACKER_CARD_JS = (
    r"""(opts) => {
    const {selector, jobId, mark} = opts;
"""
    + _RAIL_PICK_JS
    + r"""
    const main = document.querySelector('main');
    if (!main) return null;
    const idsOf = (scope) => {
        const ids = new Set();
        for (const node of scope.querySelectorAll(selector)) {
            const id = idOf(node);
            if (id) ids.add(id);
        }
        return ids;
    };
    const visible = (el) => {
        const box = el.getBoundingClientRect();
        return box.width > 0 && box.height > 0;
    };
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }
    const listed = idsOf(main);
    const menus = [];
    for (const button of main.querySelectorAll('button[aria-expanded][aria-label]')) {
        if (!visible(button)) continue;
        let card = button.parentElement;
        while (card && card !== main && idsOf(card).size === 0) {
            card = card.parentElement;
        }
        if (!card || card === main) continue;
        const ids = idsOf(card);
        if (ids.size === 1 && ids.has(jobId)) menus.push(button);
    }
    if (menus.length === 1) menus[0].setAttribute(mark, '');
    return {cards: listed.size, listed: listed.has(jobId), menus: menus.length};
}"""
)

# The item of the open overflow menu whose whole text is ``label``. The items
# are `[role="button"]`s inside a visible `[role="menu"]`, and the text comes
# from the locale table. Marked for the click only when exactly one matches.
TRACKER_MENU_ITEM_JS = r"""(opts) => {
    const {label, mark} = opts;
    const visible = (el) => {
        const box = el.getBoundingClientRect();
        return box.width > 0 && box.height > 0;
    };
    const menus = [...document.querySelectorAll('[role="menu"]')].filter(visible);
    const items = [];
    for (const menu of menus) {
        for (const item of menu.querySelectorAll('[role="button"], [role="menuitem"]')) {
            if ((item.innerText || '').trim() === label) items.push(item);
        }
    }
    if (items.length === 1) items[0].setAttribute(mark, '');
    return {menus: menus.length, items: items.length};
}"""

# The pager under a tracker tab. LinkedIn drops `start=` from the tracker's
# address (#764), so every page after the first is reachable only by clicking
# its number. Numbers are numerals in every locale the account can serve,
# unlike the "Next" beside them, so they are the only text read here. A lone
# numeral elsewhere in the tab cannot be mistaken for a pager: a group counts
# as one only when it offers both 1 and 2, and a page is clicked only when
# exactly one element carries its number.
TRACKER_PAGER_JS = r"""(opts) => {
    const {mark, page} = opts;
    const main = document.querySelector('main');
    if (!main) return null;
    const visible = (el) => {
        const box = el.getBoundingClientRect();
        return box.width > 0 && box.height > 0;
    };
    for (const old of document.querySelectorAll('[' + mark + ']')) {
        old.removeAttribute(mark);
    }
    const numbered = new Map();
    for (const el of main.querySelectorAll('button, [role="button"], a')) {
        if (!visible(el)) continue;
        if (!/^[0-9]{1,3}$/.test((el.innerText || '').trim())) continue;
        const number = parseInt((el.innerText || '').trim(), 10);
        if (!number) continue;
        numbered.set(number, [...(numbered.get(number) || []), el]);
    }
    if (!numbered.has(1) || !numbered.has(2)) return {pages: [], marked: false};
    const wanted = numbered.get(page) || [];
    if (wanted.length === 1) wanted[0].setAttribute(mark, '');
    return {
        pages: [...numbered.keys()].sort((a, b) => a - b),
        marked: wanted.length === 1,
    };
}"""

# The attributes that tie a click to the element the read chose, set in the
# breath before it, so a re-render in between fails the click rather than
# landing it on another card.
_TRACKER_MENU_MARK = "data-mcp-tracker-menu"
_TRACKER_ITEM_MARK = "data-mcp-tracker-item"
_TRACKER_PAGER_MARK = "data-mcp-tracker-pager"

# How long a tracker tab gets to render its cards, the menu to open, the
# pager to serve another page, and the card to leave the list once its item is
# clicked.
_TRACKER_READY_TIMEOUT = 10.0
_TRACKER_MENU_TIMEOUT = 5.0
_TRACKER_REMOVAL_TIMEOUT = 10.0
_TRACKER_POLL = 0.25
# How many pages of a tab one call walks. The tracker holds an account's whole
# history, and a job it does not list is only proven absent by the last page.
_TRACKER_MAX_PAGES = 10

TrackerCardOutcome = Literal[
    # The item was clicked and the card left the list.
    "removed",
    # The tab lists fewer cards than a page holds and none is this job's.
    "absent",
    # The pages read did not hold this job and more were left: the page
    # budget ran out, or the pager stopped answering.
    "beyond_first_page",
    # The job is listed but no single overflow menu could be told to be its.
    "no_menu",
    # The menu opened without the item, as an applied card's has no Unsave.
    "no_item",
    # The item was clicked and the card stayed.
    "not_removed",
]


TrackerPageMove = Literal[
    # The pager was clicked and the list it serves changed.
    "advanced",
    # The pager offers no such page: the tab ends where it is.
    "exhausted",
    # No pager at all, so nothing says whether the tab has more pages.
    "unpaged",
    # The page exists but could not be shown: its number is behind an
    # ellipsis, or the list did not change after the click.
    "stalled",
]


@dataclass(frozen=True, slots=True)
class TrackerListing:
    """What one tracker tab showed of one job."""

    listed: bool
    cards: int


@dataclass(frozen=True, slots=True)
class TrackerCardAction:
    """What acting on one tracker card came to, and how far the walk went.

    ``pages_read`` is what a caller rechecking the tab needs: a job found on
    the first page is proven gone by the first page, and walking the rest to
    say so would cost a click per page on every action.
    """

    outcome: TrackerCardOutcome
    pages_read: int


@dataclass(frozen=True, slots=True)
class JobPageCapture:
    """One job-list page read, plus what the reader alone can still answer.

    Everything a page attempt used to leave behind on the extractor travels
    here instead. That was one field, `_scroll_seconds`, and it was the only
    state crossing the page-attempt boundary without being a parameter or a
    return value; the budgets, the offset, the seen ids and the warnings were
    always the workflow's own locals and stay there.

    `landed_url` is where the browser actually is when the attempt ends, taken
    inside the reader because that is the same read the caller would make:
    nothing between this value and the caller's first look at the address
    awaits anything. The saved-jobs offset check is the one place that may not
    use it, because a page-count read runs first and can move the address; see
    `current_url`.
    """

    section: ExtractedSection
    landed_url: str
    scroll_seconds: float


@dataclass(slots=True)
class _ScrollCharge:
    """What the sidebar scroll has spent on one page, across both attempts.

    Mutable and private, unlike the capture built from it: the scroll books
    its own time in a `finally`, so an attempt that raises after scrolling
    still has to be charged, and a return value cannot carry that. It never
    leaves this module.
    """

    seconds: float = 0.0


class JobPageReader:
    """Read one job-search or saved-job page at a time.

    A page service under the job workflows rather than a peer of them. It
    consumes `PageNavigator` for navigation watching, document-origin checks
    and settling, and answers with a `JobPageCapture`; which page to ask for
    next, and what an answer means, belong to `jobs.JobReader`.
    """

    def __init__(
        self,
        session: PageSession,
        navigator: PageNavigator,
        content: PageContentReader,
    ):
        self._session = session
        self._navigator = navigator
        self._content = content

    @property
    def current_url(self) -> str:
        """Where the browser is right now, with nothing awaited to find out.

        The saved-jobs offset check needs the address as it stands *after* the
        page-count read, which the capture's `landed_url` predates. Neither an
        await nor a document-identity check on purpose: adding either would be
        a new observation on a path that has none today.
        """
        return self._session.page.url

    def _captured(
        self, section: ExtractedSection, scroll_seconds: float = 0.0
    ) -> JobPageCapture:
        """Seal one page attempt together with where it ended."""
        return JobPageCapture(
            section=section,
            landed_url=self._session.page.url,
            scroll_seconds=scroll_seconds,
        )

    async def read_apply_link(
        self, url: str, job_id: str, text: JobApplyTextTable
    ) -> JobApplyRead:
        """Read how a posting takes applications, and an external one's address.

        Nothing is clicked. An external Apply is a link whose href names the
        employer's address; a posting whose Apply is anything else answers
        `unknown`.

        The employer's address is answered as LinkedIn gives it and never
        loaded. It comes from whoever posted the job, and loading it would let
        a stranger's link, or any redirect behind it, send a request from this
        host to whatever the host can reach.
        """
        await self._navigator._navigate_to_page(url)
        await self._session.check_rate_limit()
        page = self._session.page
        opts = {
            "applyPath": f"/jobs/view/{job_id}/apply",
            "redirectPath": SAFETY_REDIRECT_PATH,
            "easyApplyLabel": text.easy_apply_label,
            "externalLabel": text.external_apply_label,
            "descriptionHeadings": list(text.description_headings),
            "closedLines": list(text.closed_lines),
            "appliedPattern": text.applied_pattern.pattern,
        }
        try:
            await page.wait_for_function(
                APPLY_READY_JS, arg=opts, timeout=_APPLY_READY_TIMEOUT * 1000
            )
        except PlaywrightTimeoutError:
            logger.debug("No apply control or posting state rendered on %s", url)

        signals = await self._session.run_on_linkedin(APPLY_SIGNALS_JS, opts)
        # Both states before any control, so a posting in either never reads
        # as open.
        if signals and signals["applied"]:
            return JobApplyRead("applied")
        if signals and signals["closed"]:
            return JobApplyRead("closed")
        if signals and signals["easy_apply"]:
            return JobApplyRead("easy_apply")
        if signals and signals["external_link"]:
            return JobApplyRead(
                "external", employer_apply_url(signals["external_link"])
            )
        # A barrier served in place of the posting renders none of the above
        # either, and it needs the relogin path rather than a type.
        await self._navigator._raise_if_auth_barrier(url)
        return JobApplyRead("unknown")

    async def _extract_job_ids(self, *, scoped: bool = False) -> list[str]:
        """Extract unique job IDs from job card links on the current page.

        Finds all `a[href*="/jobs/view/"]` links and extracts the numeric
        job ID from each href. Returns deduplicated IDs in DOM order.

        Args:
            scoped: Read only the results rail, chosen by the same rule the
                sidebar scroll uses. Off for lists that have no rail.
        """
        result = await self._session.run_on_linkedin(
            JOB_IDS_JS, {"selector": _JOB_CARD_SELECTOR, "scoped": scoped}
        )
        if scoped and not result["scoped"]:
            # The whole document, because a page with nothing scrollable
            # rendered everything it has and returning no ids at all would
            # lose the results along with the detail pane's links. Said out
            # loud, because it is the one path where the offset can count
            # something the rail never showed, and it has not been observed:
            # live a search page has two scrollable candidates.
            logger.warning(
                "No results rail on %s, reading job ids from the whole document",
                self._session.page.url,
            )
        return result["ids"]

    async def _extract_promoted_job_ids(self, label: str) -> list[str]:
        """Ids of the results rail's cards that carry ``label`` as a line.

        Raises when the page answers with anything but a list, so a caller
        treating this as best effort cannot mistake a failed read for a page
        without promoted jobs.
        """
        result = await self._session.run_on_linkedin(
            PROMOTED_JOB_IDS_JS, {"selector": _JOB_CARD_SELECTOR, "label": label}
        )
        if not isinstance(result, list):
            raise TypeError(f"Promoted job ids read returned {type(result).__name__}")
        return [job_id for job_id in result if isinstance(job_id, str)]

    async def _count_organic_before_notice(self, notice: str, label: str) -> int | None:
        """Unpromoted results above ``notice``, or None without the notice.

        Raises when the page answers with anything else, so a caller treating
        this as best effort cannot read a failed read as zero, which would
        discard the page as a no-match.
        """
        result = await self._session.run_on_linkedin(
            ORGANIC_BEFORE_NOTICE_JS,
            {"selector": _JOB_CARD_SELECTOR, "label": label, "notice": notice},
        )
        if result is None:
            return None
        if isinstance(result, bool) or not isinstance(result, int):
            raise TypeError(f"Organic results read returned {type(result).__name__}")
        return result

    async def _extract_search_page(
        self,
        url: str,
        section_name: str,
        scroll_deadline: float = SCROLL_DEADLINE_MAX,
        page_deadline: float | None = None,
    ) -> JobPageCapture:
        """Extract innerText from a job search page with soft rate-limit retry.

        Mirrors the noise-only detection and single-retry behavior of
        ``SectionCapture`` so that callers get a ``RATE_LIMITED_SECTION_TEXT``
        sentinel instead of silent empty results.

        One charge for both attempts, and it survives an attempt that raises:
        the scroll below books what it spent in a ``finally``, and the error
        path here still answers with a capture the caller charges its budget
        from. Losing that let a page whose extraction failed after a full
        twelve-second scroll cost the search nothing.

        ``page_deadline`` is where the search's budget ends, on the monotonic
        clock. Both attempts navigate and scroll against it, and the retry is
        not started when its backoff and a page will not fit before it.
        """
        charge = _ScrollCharge()
        try:
            result = await self._extract_search_page_once(
                url,
                section_name,
                scroll_deadline,
                charge=charge,
                page_deadline=page_deadline,
            )
            if result.text != RATE_LIMITED_SECTION_TEXT:
                return self._captured(result, charge.seconds)

            if page_deadline is not None:
                left = page_deadline - time.monotonic()
                if left < RATE_LIMIT_RETRY_DELAY + _RETRY_PAGE_ALLOWANCE:
                    logger.info(
                        "Not retrying search page %s: %.1fs of the budget left",
                        url,
                        left,
                    )
                    return self._captured(result, charge.seconds)

            logger.info(
                "Retrying search page %s after %.0fs backoff",
                url,
                RATE_LIMIT_RETRY_DELAY,
            )
            await asyncio.sleep(RATE_LIMIT_RETRY_DELAY)
            result = await self._extract_search_page_once(
                url,
                section_name,
                scroll_deadline / 2,
                charge=charge,
                page_deadline=page_deadline,
            )
            if result.text == RATE_LIMITED_SECTION_TEXT:
                logger.warning("Search page %s still rate-limited after retry", url)
            return self._captured(result, charge.seconds)

        except LinkedInOperationError:
            raise
        except Exception as e:
            logger.warning("Failed to extract search page %s: %s", url, e)
            return self._captured(
                ExtractedSection(
                    text="",
                    references=[],
                    error=build_issue_diagnostics(
                        e,
                        context="extract_search_page",
                        target_url=url,
                        section_name=section_name,
                    ),
                ),
                charge.seconds,
            )

    async def _extract_search_page_once(
        self,
        url: str,
        section_name: str,
        scroll_deadline: float = SCROLL_DEADLINE_MAX,
        *,
        charge: _ScrollCharge,
        page_deadline: float | None = None,
    ) -> ExtractedSection:
        """Single attempt to navigate, scroll sidebar, and extract innerText."""
        await self._navigator._navigate_to_page(url, deadline=page_deadline)
        await detect_rate_limit(self._session.page)
        # Above the selector wait and the modal close, so the window this
        # opens covers everything read from here on. Taken between them, a
        # reload committing during either one became the baseline itself, and
        # `main_found` then described a document that no longer existed.
        origin = await self._navigator._document_origin()

        main_found = True
        try:
            await self._session.page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("No <main> element found on %s", url)
            main_found = False

        await handle_modal_close(self._session.page)

        # `scroll_job_sidebar` swallows whatever its evaluate raises, so that a
        # rail replaced mid-flight does not cost the caller the page it is
        # about to read. A navigation destroys that context the same way and is
        # not the same thing: what waits to be read is then an authwall or a
        # checkpoint, and extracting it returns login text under
        # `search_results` with nothing beside it to say so.
        #
        # The route is compared as well as watched, because a redirect can
        # finish before the listener is registered. Host and path, and not the
        # whole URL, because LinkedIn appends `currentJobId` to the query of a
        # search page by itself. Measured across three live searches: the path
        # never moved, and neither did the query. The host has to come along,
        # or a redirect that keeps the path reads as no redirect at all.
        #
        # Against the URL that was asked for, and not the one the page held
        # after navigating, or a redirect finishing before the scroll becomes
        # its own baseline and passes. Outside the `main_found` branch for the
        # same reason: a landing page with no `<main>` extracts to nothing, and
        # an empty section is what an exhausted search looks like.
        before = route(url)
        moved = False
        navigated = False
        with self._navigator._watching_navigations() as hops:
            if main_found:
                scroll_started = time.monotonic()
                if page_deadline is not None:
                    # The scroll deadline was worked out before navigating.
                    # A slow `goto`, the `<main>` wait and the modal close all
                    # spend time it does not know about, and the scroll's own
                    # clock only starts when it is called.
                    scroll_deadline = min(
                        scroll_deadline, max(0.0, page_deadline - scroll_started)
                    )
                try:
                    moved = await scroll_job_sidebar(
                        self._session.page, deadline=scroll_deadline
                    )
                finally:
                    # Only what the scroll spent. Charging the whole page
                    # charged navigation and extraction to a budget that
                    # exists to bound scrolling, so five slow navigations that
                    # scrolled instantly still left the pages behind them with
                    # nothing. Accumulated, because a retry scrolls a second
                    # time.
                    charge.seconds += time.monotonic() - scroll_started
            # `hops` is read and not waited on, so a healthy page pays
            # nothing for it. It is what a scroll that finished cleanly leaves
            # behind when the document was replaced anyway: the scroll never
            # raised, so it reports no movement, and a reload moves no route,
            # so neither of the other two says anything happened.
            if moved or hops or before != route(self._session.page.url):
                navigated = await self._navigator._settle_navigation(hops, origin)

        after = route(self._session.page.url)
        if navigated or moved or not main_found or before != after:
            # Any of the three is enough, and none implies the others. A reload
            # keeps the address, so an account picker served in place of the
            # search page changes nothing the comparison below can see; a
            # redirect that completed during the navigation moves the route
            # without the scroll ever raising; and a barrier page carries no
            # `<main>`, so the scroll it would have raised from never ran.
            # That third one is the shape this check exists for and the one it
            # missed: an exhausted search renders no `<main>` either, which is
            # why the check has to decide it rather than the absence alone.
            await self._navigator._raise_if_auth_barrier(url)
        if before != after and not same_job_search(before, after):
            # An expired session lands here as often as a layout change does,
            # and the two need different answers. A plain error is caught by
            # the generic handler above and returned as a section diagnostic,
            # so the browser stays registered and no re-login is offered; the
            # caller then repeats the search against the same barrier.
            raise RuntimeError(
                f"Page navigated to {self._session.page.url} while scrolling {url}"
            )

        raw_result = await self._content._extract_root_content(["main"])

        # The watcher covers the scroll and nothing else, and the read sits
        # outside it at both ends: a reload committing after the listener came
        # off, or during the extraction itself, moves no route and raises
        # nothing. The document says what neither the address nor the listener
        # can, and it is asked about the text that was actually read.
        if origin is not None and await self._navigator._document_origin() != origin:
            logger.debug("The search document was replaced before it was read")
            await self._navigator._raise_if_auth_barrier(url)

        raw = raw_result["text"]
        if raw_result["source"] == "body":
            logger.debug("No <main> at evaluation time on %s, using body fallback", url)
        elif not main_found:
            logger.debug(
                "<main> appeared after wait timeout on %s, sidebar scroll was skipped",
                url,
            )

        if not raw:
            return ExtractedSection(text="", references=[])
        truncated = truncate_linkedin_noise(raw)
        if not truncated and raw.strip():
            logger.warning(
                "Search page %s returned only LinkedIn chrome (likely rate-limited)",
                url,
            )
            return ExtractedSection(text=RATE_LIMITED_SECTION_TEXT, references=[])
        cleaned = filter_linkedin_noise_lines(truncated)
        return ExtractedSection(
            text=cleaned,
            references=build_references(
                raw_result["references"], section_name, apply_cap=False
            ),
        )

    async def _get_total_search_pages(self) -> int | None:
        """Read total page count from LinkedIn's pagination state element.

        Parses the "Page X of Y" text from ``.jobs-search-pagination__page-state``.
        Returns ``None`` when the element is absent or unparseable.

        NOTE: This is a deliberate DOM exception. The element has ``display: none``
        (screen-reader only), so the text never appears in ``innerText``. A class-based
        selector is the only reliable way to read it. Gracefully returns ``None`` if
        LinkedIn renames the class — pagination just falls back to ``max_pages``.
        """
        text = await self._session.run_on_linkedin(
            """() => {
                const el = document.querySelector(
                    '.jobs-search-pagination__page-state'
                );
                return el ? el.textContent.trim() : null;
            }"""
        )
        if not text:
            return None
        match = re.search(r"of\s+(\d+)", text)
        return int(match.group(1)) if match else None

    async def _extract_saved_jobs_page(
        self,
        url: str,
        section_name: str,
    ) -> JobPageCapture:
        """Extract innerText from a saved-jobs page with soft rate-limit retry."""
        with self._navigator._watching_navigations() as hops:
            try:
                result = await self._extract_saved_jobs_page_once(url, section_name)
                if result.text != RATE_LIMITED_SECTION_TEXT:
                    return self._captured(result)

                logger.info(
                    "Retrying saved jobs page %s after %.0fs backoff",
                    url,
                    RATE_LIMIT_RETRY_DELAY,
                )
                await asyncio.sleep(RATE_LIMIT_RETRY_DELAY)
                result = await self._extract_saved_jobs_page_once(url, section_name)
                if result.text == RATE_LIMITED_SECTION_TEXT:
                    logger.warning(
                        "Saved jobs page %s still rate-limited after retry", url
                    )
                return self._captured(result)

            except LinkedInOperationError:
                raise
            except Exception as e:
                logger.warning("Failed to extract saved jobs page %s: %s", url, e)
                # A navigation destroys the scroll's execution context, and
                # what waits behind it is a checkpoint as often as a layout
                # change. Turning that into a section diagnostic hands the
                # caller an empty list, leaves the browser registered and
                # offers no relogin, so the next call meets the same barrier.
                #
                # Whether one happened is the listener's answer and not the
                # address's: this list reaches `/jobs-tracker/` by a redirect
                # LinkedIn makes on purpose, so comparing against the URL that
                # was asked for finds a difference on every ordinary failure
                # and waits out a chain that is not running.
                #
                # No document baseline, so every hop counts. One is taken
                # before the search scroll, where the page is already loaded
                # and the only navigation to expect is one going wrong. Here
                # the block opens before this page's own navigation, so a
                # reading from the top belongs to the document that was left
                # and would call every ordinary failure a replacement. `None`
                # says so, and settling costs a moment on a path that has
                # already failed.
                try:
                    await self._navigator._settle_navigation(hops, None)
                except Exception:
                    logger.debug(
                        "Could not settle the route after a saved-jobs failure",
                        exc_info=True,
                    )
                await self._navigator._raise_if_auth_barrier(
                    self._session.page.url, navigation_error=e
                )
                return self._captured(
                    ExtractedSection(
                        text="",
                        references=[],
                        error=build_issue_diagnostics(
                            e,
                            context="extract_saved_jobs_page",
                            target_url=url,
                            section_name=section_name,
                        ),
                    )
                )

    async def _extract_saved_jobs_page_once(
        self,
        url: str,
        section_name: str,
    ) -> ExtractedSection:
        """Single attempt: navigate, scroll list, and extract innerText."""
        await self._navigator._navigate_to_page(url)
        return await self._read_tracker_document(url, section_name)

    async def read_shown_tracker_page(self, section_name: str) -> JobPageCapture:
        """Read the tracker page the browser is already on.

        What the pager leaves behind: a page it clicked into has no address of
        its own, so it is read where it stands, with the same guards as a page
        that was navigated to and no rate-limit retry, which would need a
        navigation that would lose the page.
        """
        return self._captured(
            await self._read_tracker_document(self._session.page.url, section_name)
        )

    async def _read_tracker_document(
        self,
        url: str,
        section_name: str,
    ) -> ExtractedSection:
        """Scroll the tracker list the browser is on and extract its innerText."""
        await detect_rate_limit(self._session.page)
        # Taken after this page's own navigation, so it belongs to the
        # document about to be read rather than to the one that was left.
        origin = await self._navigator._document_origin()

        main_found = True
        try:
            await self._session.page.wait_for_selector("main")
        except PlaywrightTimeoutError:
            logger.debug("No <main> element found on %s", url)
            main_found = False

        await handle_modal_close(self._session.page)
        if main_found:
            await scroll_to_bottom(self._session.page, pause_time=0.5, max_scrolls=5)
        else:
            # A picker served in place of the list keeps the list's address
            # and its title, so the route guard below sees an allowed page and
            # the body fallback returns the picker under `saved_jobs`. Missing
            # `<main>` is what is left, and an emptied list has none either,
            # which is why the check decides it rather than the absence.
            await self._navigator._raise_if_auth_barrier(self._session.page.url)

        # A picker served by a reload keeps this page's address and this
        # page's title, so the route guard reads it as the list. Nothing else
        # notices either: the scroll pauses half a second between rounds, and
        # a document replaced in that gap leaves no evaluation to raise, so
        # the extraction succeeds against the replacement and returns it under
        # `saved_jobs` with the browser left on a barrier.
        #
        # Asked after the read rather than before it, or the gap between the
        # two is a window of its own and the text that came back is not the
        # text the check judged.
        raw_result = await self._content._extract_root_content(["main"])
        if origin is not None and await self._navigator._document_origin() != origin:
            logger.debug("The saved-jobs document was replaced before it was read")
            await self._navigator._raise_if_auth_barrier(self._session.page.url)
        raw = raw_result["text"]
        if raw_result["source"] == "body":
            logger.debug("No <main> at evaluation time on %s, using body fallback", url)
        elif not main_found:
            logger.debug(
                "<main> appeared after wait timeout on %s, scroll was skipped",
                url,
            )

        if not raw:
            return ExtractedSection(text="", references=[])
        truncated = truncate_linkedin_noise(raw)
        if not truncated and raw.strip():
            logger.warning(
                "Saved jobs page %s returned only LinkedIn chrome (likely rate-limited)",
                url,
            )
            return ExtractedSection(text=RATE_LIMITED_SECTION_TEXT, references=[])
        cleaned = filter_linkedin_noise_lines(truncated)
        return ExtractedSection(
            text=cleaned,
            references=build_references(raw_result["references"], section_name),
        )

    async def _get_total_list_pages(self) -> int | None:
        """Read last page number from artdeco pagination buttons.

        Parses numeric page labels from ``ul.artdeco-pagination__pages``.
        Returns ``None`` when pagination is absent or unparseable.

        NOTE: This is a deliberate DOM exception, mirroring
        ``_get_total_search_pages``. The my-items pager exposes no page count
        in ``innerText`` and no stable attribute to count, so a design-system
        class is the only reachable signal. The labels are numerals rather
        than words, so no locale table is needed. A renamed class, or a locale
        serving non-ASCII numerals that ``parseInt`` cannot read, both yield
        ``None`` — pagination then falls back to ``max_pages`` and the
        no-new-ids early stop.
        """
        value = await self._session.run_on_linkedin(
            """() => {
                const buttons = document.querySelectorAll(
                    'ul.artdeco-pagination__pages li button'
                );
                if (!buttons.length) return null;
                const nums = [...buttons]
                    .map((b) => parseInt(b.textContent.trim(), 10))
                    .filter((n) => !Number.isNaN(n));
                return nums.length ? Math.max(...nums) : null;
            }"""
        )
        return int(value) if value is not None else None

    async def _open_tracker_stage(self, stage: JobsTrackerStage) -> None:
        """Navigate to a tracker tab and wait for its cards to render.

        Raises when the navigation ends anywhere but that tab, after offering
        the page to the auth-barrier check, because a tab that is not the one
        asked for holds cards that are not the ones meant. A tab with no cards
        renders none, so the wait for a card ends in silence rather than an
        error and the caller counts what is there.
        """
        await self._navigator._navigate_to_page(tracker_stage_url(stage))
        await self._session.check_rate_limit()
        page = self._session.page
        if not on_tracker_stage(page.url, stage):
            await self._navigator._raise_if_auth_barrier(page.url)
            raise LinkedInOperationError(
                f"The job tracker's {stage} tab navigation ended on {page.url}"
            )
        await self._session.dismiss_modal()
        try:
            await page.wait_for_selector(
                f"main :is({_JOB_CARD_SELECTOR})",
                # Attached rather than visible: every card is rendered twice,
                # once in a collapsed layout with no size, and that one can
                # come first.
                state="attached",
                timeout=int(_TRACKER_READY_TIMEOUT * 1000),
            )
        except PlaywrightTimeoutError:
            logger.debug("No job card rendered on the tracker's %s tab", stage)

    async def _find_tracker_card(self, job_id: str) -> dict[str, int | bool]:
        """Count the tab's cards and mark ``job_id``'s overflow menu."""
        found = await self._session.page.evaluate(
            TRACKER_CARD_JS,
            {
                "selector": _JOB_CARD_SELECTOR,
                "jobId": job_id,
                "mark": _TRACKER_MENU_MARK,
            },
        )
        if not isinstance(found, dict):
            await self._navigator._raise_if_auth_barrier(self._session.page.url)
            raise LinkedInOperationError(
                "The job tracker page has no main content to read"
            )
        return found

    async def advance_tracker_page(self, page_number: int) -> TrackerPageMove:
        """Click the tracker's pager to show its ``page_number``-th page.

        The pager is the tracker's only way forward, since the tab drops
        `start=` from its address. Success is the list answering with other
        jobs: the pager renders the page it is on as a number too, so a click
        that changed nothing is indistinguishable from one that was ignored,
        and both are reported as a stall rather than as a page.
        """
        pager = await self._session.page.evaluate(
            TRACKER_PAGER_JS, {"mark": _TRACKER_PAGER_MARK, "page": page_number}
        )
        if not isinstance(pager, dict) or not pager["pages"]:
            return "unpaged"
        if not any(int(number) >= page_number for number in pager["pages"]):
            return "exhausted"
        if not pager["marked"]:
            return "stalled"

        before = set(await self._extract_job_ids())
        await self._session.page.locator(f"main [{_TRACKER_PAGER_MARK}]").click(
            timeout=5000
        )
        deadline = self._session.monotonic() + _TRACKER_READY_TIMEOUT
        while self._session.monotonic() < deadline:
            await self._session.delay(_TRACKER_POLL)
            shown = set(await self._extract_job_ids())
            if shown and shown != before:
                return "advanced"
        return "stalled"

    async def read_tracker_listing(
        self, stage: JobsTrackerStage, job_id: str, max_pages: int = _TRACKER_MAX_PAGES
    ) -> TrackerListing:
        """Whether a tracker tab lists ``job_id``, walking its pages.

        ``cards`` counts every card seen on the way, so a caller can tell an
        empty tab from one whose pages were all read without the job.
        """
        await self._open_tracker_stage(stage)
        found = await self._find_tracker_card(job_id)
        cards = int(found["cards"])
        pages_read = 1
        while not found["listed"] and pages_read < max_pages:
            if await self.advance_tracker_page(pages_read + 1) != "advanced":
                break
            pages_read += 1
            found = await self._find_tracker_card(job_id)
            cards += int(found["cards"])
        return TrackerListing(listed=bool(found["listed"]), cards=cards)

    async def act_on_tracker_card(
        self,
        stage: JobsTrackerStage,
        job_id: str,
        item: str,
        max_pages: int = _TRACKER_MAX_PAGES,
    ) -> TrackerCardAction:
        """Choose ``item`` from ``job_id``'s overflow menu on a tracker tab.

        The tab's pages are walked until they hold the job or run out, since
        the tracker drops `start=` (#764) and no address reaches a later page.
        The card is found by structure, the item by its text from the locale
        table, and each is clicked through a mark set the moment before.
        Success is the card leaving the list; the caller confirms it against a
        fresh load of the tab.

        A job the walk never meets is ``absent`` only when the walk saw the
        end of the tab. A budget that ran out, a pager that stopped answering,
        and a full page with no pager to read past all stay
        ``beyond_first_page``: the tab may hold the job further on.
        """
        await self._open_tracker_stage(stage)
        found = await self._find_tracker_card(job_id)
        pages_read = 1
        while not found["listed"]:
            if pages_read >= max_pages:
                return TrackerCardAction("beyond_first_page", pages_read)
            move = await self.advance_tracker_page(pages_read + 1)
            if move == "exhausted":
                return TrackerCardAction("absent", pages_read)
            if move == "unpaged":
                if int(found["cards"]) < SAVED_JOBS_PAGE_SIZE:
                    return TrackerCardAction("absent", pages_read)
                return TrackerCardAction("beyond_first_page", pages_read)
            if move != "advanced":
                return TrackerCardAction("beyond_first_page", pages_read)
            pages_read += 1
            found = await self._find_tracker_card(job_id)
        if found["menus"] != 1:
            return TrackerCardAction("no_menu", pages_read)

        page = self._session.page
        await page.locator(f"main button[{_TRACKER_MENU_MARK}]").click(timeout=5000)
        deadline = self._session.monotonic() + _TRACKER_MENU_TIMEOUT
        menu: dict[str, int] = {"menus": 0, "items": 0}
        while self._session.monotonic() < deadline:
            menu = await page.evaluate(
                TRACKER_MENU_ITEM_JS, {"label": item, "mark": _TRACKER_ITEM_MARK}
            )
            if menu["items"] == 1:
                break
            await self._session.delay(_TRACKER_POLL)
        if menu["items"] != 1:
            await page.keyboard.press("Escape")
            return TrackerCardAction(
                "no_item" if menu["menus"] else "no_menu", pages_read
            )

        await page.locator(f"[{_TRACKER_ITEM_MARK}]").click(timeout=5000)
        deadline = self._session.monotonic() + _TRACKER_REMOVAL_TIMEOUT
        while self._session.monotonic() < deadline:
            await self._session.delay(_TRACKER_POLL)
            if not (await self._find_tracker_card(job_id))["listed"]:
                return TrackerCardAction("removed", pages_read)
        return TrackerCardAction("not_removed", pages_read)
