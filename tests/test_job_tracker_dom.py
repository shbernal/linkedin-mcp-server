"""Browser-DOM tests for acting on a job from LinkedIn's job tracker.

The unit suite mocks ``page.evaluate``, so the programs behind
``JobPageReader.act_on_tracker_card`` never execute there. These run the whole
action against a synthetic tracker tab in headless chromium, with the
navigation stubbed. The markup carries the structure measured on 2026-09-21:
every card holds its job's link and two overflow buttons with `aria-label` and
`aria-expanded`, one of them in a collapsed layout with no size, and the menu
is a `[role="menu"]` of `[role="button"]` items. None of LinkedIn's classes, so
it is a claim about which signals are read, not about LinkedIn's layout.

Skipped automatically when chromium is not installed; run locally after
``uv run patchright install chromium --no-shell``.
"""

from __future__ import annotations

import json

from unittest.mock import AsyncMock, patch

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.linkedin import job_pages
from linkedin_mcp_server.linkedin.content import PageContentReader
from linkedin_mcp_server.linkedin.job_pages import JobPageReader
from linkedin_mcp_server.linkedin.navigation import PageNavigator
from linkedin_mcp_server.linkedin.session import PageSession
from linkedin_mcp_server.linkedin.text import JOB_TRACKER_EN_US

pytestmark = [
    pytest.mark.browser_dom,
    pytest.mark.xdist_group("browser_runtime"),
]

SAVED_URL = "https://www.linkedin.com/jobs-tracker/?stage=saved"
UNSAVE = JOB_TRACKER_EN_US.unsave_item
ARCHIVE = JOB_TRACKER_EN_US.archive_item

# Opens the card's menu with the items its tab offers. Choosing one records it
# in the DOM, and removes the card unless the page says the change is refused.
# Recorded as an attribute rather than on `window`, because patchright
# evaluates in an isolated world that never sees the page's own globals.
MENU_SCRIPT = """
const chosen = [];
const wire = () => {
for (const button of document.querySelectorAll('main button[aria-expanded]')) {
    button.addEventListener('click', () => {
        document.querySelector('[role="menu"]')?.remove();
        const card = button.closest('[data-card]');
        const menu = document.createElement('div');
        menu.setAttribute('role', 'menu');
        for (const label of card.dataset.items.split(',')) {
            const item = document.createElement('div');
            item.setAttribute('role', 'button');
            item.innerHTML = '<div><p>' + label + '</p></div>';
            item.addEventListener('click', () => {
                chosen.push([card.dataset.card, label]);
                document.body.dataset.chosen = JSON.stringify(chosen);
                menu.remove();
                if (!document.body.hasAttribute('data-refuse')) card.remove();
            });
            menu.appendChild(item);
        }
        document.body.appendChild(menu);
    });
}
};
wire();
"""


def card(
    job_id: str, items: tuple[str, ...] = ("Change stage", ARCHIVE, UNSAVE)
) -> str:
    return (
        f'<li data-card="{job_id}" data-items="{",".join(items)}">'
        f'<div style="display:none"><button aria-label="Overflow menu"'
        f' aria-expanded="false">…</button></div>'
        f'<a href="https://www.linkedin.com/jobs/view/{job_id}/">Job {job_id}</a>'
        f"<p>Acme · Paris</p>"
        f'<button aria-label="Overflow menu" aria-expanded="false">…</button>'
        f"</li>"
    )


def tracker(*cards: str, script: str = MENU_SCRIPT) -> str:
    """A tracker tab: its toolbar, then the list."""
    return (
        '<main><button aria-label="Back">←</button>'
        '<div role="button" aria-expanded="false">In Progress · 1</div>'
        f"<ul>{''.join(cards)}</ul></main>"
        f"<script>{script}</script>"
    )


def paged_tracker(*pages: tuple[str, ...], script: str = MENU_SCRIPT) -> str:
    """A tracker tab whose numbered pager swaps the list, as LinkedIn's does.

    The pager is numerals and a "Next", none of LinkedIn's classes, since
    what is under test is that the numbers are what gets clicked.
    """
    lists = json.dumps(["".join(cards) for cards in pages])
    buttons = "".join(
        f"<button>{number}</button>" for number in range(1, len(pages) + 1)
    )
    return (
        '<main><button aria-label="Back">←</button>'
        f"<ul>{''.join(pages[0])}</ul>"
        f"<nav>{buttons}<button>Next</button></nav></main>"
        f"<script>{script}"
        f"const lists = {lists};"
        """
        for (const button of document.querySelectorAll('nav button')) {
            button.addEventListener('click', () => {
                const page = parseInt(button.textContent, 10);
                if (!page) return;
                document.querySelector('main ul').innerHTML = lists[page - 1];
                wire();
            });
        }
        </script>"""
    )


@pytest.fixture
async def dom_page():
    async def answer(route):
        await route.fulfill(status=200, content_type="text/html", body="<p>ok</p>")

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(
                channel="chromium", headless=True
            )
            context = await browser.new_context()
            page = await context.new_page()
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"chromium unavailable: {exc}")
        await context.route("**/*", answer)
        try:
            yield page
        finally:
            await browser.close()


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch):
    monkeypatch.setattr(job_pages, "_TRACKER_READY_TIMEOUT", 1.0)
    monkeypatch.setattr(job_pages, "_TRACKER_MENU_TIMEOUT", 1.0)
    monkeypatch.setattr(job_pages, "_TRACKER_REMOVAL_TIMEOUT", 1.0)


async def act(page, html: str, job_id: str, item: str = UNSAVE) -> str:
    """Serve ``html`` as the saved tab and act on it the way the workflow does."""
    await page.goto(SAVED_URL)
    await page.set_content(html)
    session = PageSession(page)
    reader = JobPageReader(session, PageNavigator(session), PageContentReader(session))
    with patch.object(reader._navigator, "_navigate_to_page", new_callable=AsyncMock):
        return (await reader.act_on_tracker_card("saved", job_id, item)).outcome


async def chosen(page) -> list[list[str]]:
    return await page.evaluate("() => JSON.parse(document.body.dataset.chosen || '[]')")


async def test_the_item_is_chosen_from_the_jobs_own_menu(dom_page):
    html = tracker(card("111"), card("222"), card("333"))

    assert await act(dom_page, html, "222") == "removed"
    assert await chosen(dom_page) == [["222", UNSAVE]]
    assert await dom_page.locator("[data-card]").count() == 2


async def test_a_menu_without_the_item_chooses_nothing(dom_page):
    """An applied card's menu has no Unsave, and Archive is not taken for it."""
    html = tracker(card("111", ("Change stage", ARCHIVE, "Delete")))

    assert await act(dom_page, html, "111") == "no_item"
    assert await chosen(dom_page) == []


async def test_a_short_list_without_the_job_does_not_hold_it(dom_page):
    html = tracker(card("111"), card("222"))

    assert await act(dom_page, html, "999") == "absent"
    assert await chosen(dom_page) == []


async def test_an_empty_tab_does_not_hold_it(dom_page):
    assert await act(dom_page, tracker(), "999") == "absent"


async def test_a_full_page_without_the_job_may_hold_it_later(dom_page):
    html = tracker(*(card(str(100 + index)) for index in range(10)))

    assert await act(dom_page, html, "999") == "beyond_first_page"
    assert await chosen(dom_page) == []


async def test_a_card_that_stays_is_not_reported_removed(dom_page):
    html = tracker(
        card("111"),
        script="document.body.setAttribute('data-refuse', '');" + MENU_SCRIPT,
    )

    assert await act(dom_page, html, "111") == "not_removed"
    assert await chosen(dom_page) == [["111", UNSAVE]]


async def test_a_card_holding_two_jobs_is_not_either_jobs_card(dom_page):
    """A menu is a job's only when its card links to that job and no other."""
    two = card("111").replace(
        "<p>Acme · Paris</p>",
        '<a href="https://www.linkedin.com/jobs/view/222/">Similar</a>',
    )
    html = tracker(two, card("333"))

    assert await act(dom_page, html, "111") == "no_menu"
    assert await chosen(dom_page) == []


async def test_an_item_offered_twice_is_not_guessed_at(dom_page):
    html = tracker(card("111", (ARCHIVE, UNSAVE, UNSAVE)))

    assert await act(dom_page, html, "111") == "no_item"
    assert await chosen(dom_page) == []


async def test_a_job_on_a_later_page_is_reached_through_the_pager(dom_page):
    """The tracker has no address for page two, so the pager is the way in."""
    html = paged_tracker(
        tuple(card(str(100 + index)) for index in range(10)),
        (card("222"), card("333")),
    )

    assert await act(dom_page, html, "222") == "removed"
    assert await chosen(dom_page) == [["222", UNSAVE]]


async def test_a_job_on_no_page_of_the_tab_is_absent(dom_page):
    """Walked to the last page, a job the tab never listed is not there."""
    html = paged_tracker(
        tuple(card(str(100 + index)) for index in range(10)),
        (card("222"),),
    )

    assert await act(dom_page, html, "999") == "absent"
    assert await chosen(dom_page) == []


async def test_a_pager_that_does_not_move_the_list_holds_the_job_back(dom_page):
    """A click that changes nothing is not a page, and not an answer either.

    The short first page is what makes the two apart: a tab with no pager
    under it would be answered ``absent``, and this one is not.
    """
    html = paged_tracker(
        (card("111"), card("112")),
        (card("222"),),
    ).replace("document.querySelector('main ul').innerHTML = lists[page - 1];", "")

    assert await act(dom_page, html, "222") == "beyond_first_page"
    assert await chosen(dom_page) == []


async def test_the_page_budget_stops_the_walk(dom_page):
    """A tab with more pages than the budget cannot prove a job absent."""
    html = paged_tracker(
        tuple(card(str(100 + index)) for index in range(10)),
        (card("222"),),
    )

    await dom_page.goto(SAVED_URL)
    await dom_page.set_content(html)
    session = PageSession(dom_page)
    reader = JobPageReader(session, PageNavigator(session), PageContentReader(session))
    with patch.object(reader._navigator, "_navigate_to_page", new_callable=AsyncMock):
        action = await reader.act_on_tracker_card("saved", "222", UNSAVE, max_pages=1)

    assert action.outcome == "beyond_first_page"


async def test_a_lone_numeral_is_not_a_pager(dom_page):
    """One number under a full list says nothing about a second page."""
    html = tracker(*(card(str(100 + index)) for index in range(10))).replace(
        "</main>", "<button>1</button></main>"
    )

    assert await act(dom_page, html, "999") == "beyond_first_page"


async def test_a_listing_read_walks_the_pages_too(dom_page):
    """archive_job asks the archived tab whether the job is there at all."""
    html = paged_tracker(
        tuple(card(str(100 + index)) for index in range(10)),
        (card("222"),),
    )
    await dom_page.goto(SAVED_URL)
    await dom_page.set_content(html)
    session = PageSession(dom_page)
    reader = JobPageReader(session, PageNavigator(session), PageContentReader(session))
    with patch.object(reader._navigator, "_navigate_to_page", new_callable=AsyncMock):
        listing = await reader.read_tracker_listing("saved", "222")

    assert listing.listed
    assert listing.cards == 11
