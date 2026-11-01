"""The debug switch that holds the page's messaging writes.

It exists to reach ``send_unconfirmed`` live, so what matters is that a held
write really is held, that reads are not, and that an unreadable setting never
degrades into no hold at all (which would send the message for real).
"""

from __future__ import annotations

import logging
import time

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.debug_utils import hold_message_send, message_send_hold

ENV = "LINKEDIN_DEBUG_HOLD_MESSAGE_SEND"
ORIGIN = "https://www.linkedin.com"
SEND = (
    f"{ORIGIN}/voyager/api/voyagerMessagingDashMessengerMessages?action=createMessage"
)
logger = logging.getLogger(__name__)


class TestMessageSendHold:
    def test_unset_means_no_hold(self, monkeypatch):
        monkeypatch.delenv(ENV, raising=False)
        assert message_send_hold() is None

    def test_reads_seconds_and_abort(self, monkeypatch):
        monkeypatch.setenv(ENV, " 15 ")
        assert message_send_hold() == 15.0
        monkeypatch.setenv(ENV, "Abort")
        assert message_send_hold() == "abort"

    @pytest.mark.parametrize("raw", ["abrt", "0", "-3", "nan", "inf"])
    def test_an_unreadable_value_is_refused(self, monkeypatch, raw):
        monkeypatch.setenv(ENV, raw)
        with pytest.raises(ValueError, match=ENV):
            message_send_hold()


@pytest.fixture
async def dom_page():
    """Real chromium page on a LinkedIn origin that the fixture answers."""
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            # Registered first, so it runs after the hold falls back to it.
            await page.route(
                f"{ORIGIN}/**",
                lambda route: route.fulfill(content_type="text/html", body="ok"),
            )
            await page.goto(f"{ORIGIN}/messaging/")
            yield page
        finally:
            await browser.close()


async def _fetch(page, method: str) -> tuple[str, float]:
    started = time.monotonic()
    outcome = await page.evaluate(
        """([url, method]) => fetch(url, {method}).then(
            r => r.text(), () => 'failed')""",
        [SEND, method],
    )
    return outcome, time.monotonic() - started


@pytest.mark.browser_dom
@pytest.mark.xdist_group("browser_runtime")
class TestHoldOnPage:
    async def test_abort_fails_a_messaging_write(self, dom_page, monkeypatch):
        monkeypatch.setenv(ENV, "abort")
        await hold_message_send(dom_page, logger)
        assert (await _fetch(dom_page, "POST"))[0] == "failed"

    async def test_seconds_delay_a_messaging_write(self, dom_page, monkeypatch):
        monkeypatch.setenv(ENV, "1")
        await hold_message_send(dom_page, logger)
        outcome, elapsed = await _fetch(dom_page, "POST")
        assert outcome == "ok"
        assert elapsed >= 1.0

    async def test_reads_pass_untouched(self, dom_page, monkeypatch):
        monkeypatch.setenv(ENV, "abort")
        await hold_message_send(dom_page, logger)
        assert (await _fetch(dom_page, "GET"))[0] == "ok"

    async def test_unset_installs_nothing(self, dom_page, monkeypatch):
        monkeypatch.delenv(ENV, raising=False)
        await hold_message_send(dom_page, logger)
        assert (await _fetch(dom_page, "POST"))[0] == "ok"
