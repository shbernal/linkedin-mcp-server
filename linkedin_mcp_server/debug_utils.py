"""Shared debug-only helpers for slower, traceable navigation flows."""

from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import Any, Literal

_NAV_STABILIZE_DELAY_SECONDS = 5.0

_HOLD_MESSAGE_SEND_ENV = "LINKEDIN_DEBUG_HOLD_MESSAGE_SEND"
#: Matched by path so that the page's own requests are held; nothing here calls it.
_MESSAGING_API_GLOB = "**/voyager/api/voyagerMessaging**"


def debug_stabilize_navigation_enabled() -> bool:
    """Return whether debug-only navigation stabilization sleeps are enabled."""
    return os.getenv("LINKEDIN_DEBUG_STABILIZE_NAVIGATION", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


async def stabilize_navigation(label: str, logger: logging.Logger) -> None:
    """Pause between navigation steps to help debug timing-sensitive flows."""
    if (
        os.environ.get("PYTEST_CURRENT_TEST")
        or not debug_stabilize_navigation_enabled()
    ):
        return

    logger.debug(
        "Stabilizing navigation for %.1fs after %s",
        _NAV_STABILIZE_DELAY_SECONDS,
        label,
    )
    await asyncio.sleep(_NAV_STABILIZE_DELAY_SECONDS)


def message_send_hold() -> float | Literal["abort"] | None:
    """How to hold the page's messaging writes: seconds, ``"abort"``, or not at all.

    Raises on a value it cannot read: a hold silently ignored would send the
    message for real.
    """
    raw = os.getenv(_HOLD_MESSAGE_SEND_ENV, "").strip().lower()
    if not raw:
        return None
    if raw == "abort":
        return "abort"
    try:
        seconds = float(raw)
    except ValueError:
        seconds = math.nan
    if not (seconds > 0 and math.isfinite(seconds)):
        raise ValueError(
            f"{_HOLD_MESSAGE_SEND_ENV} must be a number of seconds or 'abort', "
            f"got {raw!r}"
        )
    return seconds


async def hold_message_send(page: Any, logger: logging.Logger) -> None:
    """Delay or abort every messaging write the page makes, when the switch is set.

    A way to reach the unconfirmed-send path live: a send held past the
    confirmation wait is submitted but never confirmed. Reads are let through
    so the conversation still loads. Routing turns off the page's HTTP cache,
    so nothing is installed while the switch is unset.
    """
    hold = message_send_hold()
    if hold is None:
        return

    async def handle(route: Any) -> None:
        request = route.request
        if request.method == "GET":
            await route.fallback()
            return
        logger.warning("Debug hold (%s) on %s %s", hold, request.method, request.url)
        if hold == "abort":
            await route.abort()
            return
        await asyncio.sleep(hold)
        await route.fallback()

    await page.route(_MESSAGING_API_GLOB, handle)
