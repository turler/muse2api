"""Borrow the muse.ai login of an everyday browser (e.g. Brave) for a headless Chromium.

The browser doing the work stays headless and private to muse2api; only the session
cookies come from the user's browser profile, read over that browser's DevTools port.
Nothing is opened in the user's browser: ``Storage.getCookies`` / ``Storage.setCookies``
run on the browser target and never create a tab or take focus.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

import httpx

from .cdp import CDPSession

log = logging.getLogger(__name__)


def muse_cookies(raw: Iterable[dict[str, Any]]) -> tuple[dict[str, str], dict[str, int]]:
    """Pick the muse.ai cookies out of a CDP cookie list -> (values, expiries)."""
    cookies: dict[str, str] = {}
    expires: dict[str, int] = {}
    for c in raw:
        domain = (c.get("domain") or "").lstrip(".")
        if not (domain == "muse.ai" or domain.endswith(".muse.ai")) or not c.get("value"):
            continue
        cookies[c["name"]] = c["value"]
        if (exp := c.get("expires") or 0) > 0:
            expires[c["name"]] = int(exp)
    return cookies, expires


class CookieSource:
    """DevTools endpoint of the browser whose profile holds the muse.ai login."""

    def __init__(self, endpoint: str, launch_cmd: str = "", launch_wait: float = 45.0):
        endpoint = endpoint.strip().rstrip("/")
        if "://" not in endpoint:
            endpoint = "http://" + endpoint
        self.endpoint = endpoint
        self.launch_cmd = launch_cmd
        self.launch_wait = launch_wait
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"CookieSource({self.endpoint})"

    async def _ws_url(self) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                url = (await client.get(f"{self.endpoint}/json/version")).json().get(
                    "webSocketDebuggerUrl")
        except (httpx.HTTPError, ValueError):
            return None
        if not url:
            return None
        # Behind a tunnel the browser reports its own host:port; use ours.
        host = self.endpoint.split("://", 1)[1]
        return f"ws://{host}/devtools/{url.split('/devtools/', 1)[-1]}"

    async def _connect(self) -> CDPSession:
        url = await self._ws_url()
        if url is None and self.launch_cmd:
            log.info("cookie source %s not reachable; running %s", self.endpoint,
                     self.launch_cmd)
            proc = await asyncio.create_subprocess_shell(
                self.launch_cmd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                await asyncio.wait_for(proc.wait(), 30)
            except asyncio.TimeoutError:
                proc.kill()
            for _ in range(int(self.launch_wait / 0.5)):
                if url := await self._ws_url():
                    break
                await asyncio.sleep(0.5)
        if url is None:
            raise RuntimeError(f"cookie source browser not reachable at {self.endpoint}")
        return await CDPSession.connect(url)

    async def get_cookies(self) -> list[dict[str, Any]]:
        async with self._lock:
            session = await self._connect()
            try:
                return (await session.send("Storage.getCookies", {})).get("cookies", [])
            finally:
                await session.close()

    async def set_cookies(self, cookies: list[dict[str, Any]]) -> None:
        async with self._lock:
            session = await self._connect()
            try:
                await session.send("Storage.setCookies", {"cookies": cookies})
            finally:
                await session.close()
