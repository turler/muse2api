"""Browser process management.

Two modes (``MUSE2API_BROWSER_MODE``):

* ``launch`` (default): muse2api owns a dedicated Chromium (headless by default) with its
  own profile directory and stops it on shutdown.
* ``attach``: muse2api drives the user's everyday browser (e.g. Brave) through its
  DevTools port. When nothing answers on that port the browser is started, either with
  ``MUSE2API_BROWSER_LAUNCH_CMD`` (any shell command, e.g. one that starts Brave on another
  machine) or by running the executable with ``--remote-debugging-port``. A browser
  started this way is detached and is never closed by muse2api.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "/usr/bin/chromium",
    "/snap/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

_BRAVE_CANDIDATES = (
    "brave-browser",
    "brave",
    "/usr/bin/brave-browser",
    "/snap/bin/brave",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)


def _windows_candidates(brave: bool) -> tuple[str, ...]:
    sub = (("BraveSoftware", "Brave-Browser", "Application", "brave.exe") if brave
           else ("Google", "Chrome", "Application", "chrome.exe"))
    roots = (os.environ.get(k, "") for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"))
    return tuple(os.path.join(root, *sub) for root in roots if root)


def find_chromium(configured: str = "", prefer_brave: bool = False) -> str:
    brave = _BRAVE_CANDIDATES + _windows_candidates(True)
    others = _CANDIDATES + _windows_candidates(False)
    order = brave + others if prefer_brave else others + brave
    for c in ((configured,) if configured else ()) + order:
        if c and (os.path.isfile(c) or shutil.which(c)):
            return c
    raise FileNotFoundError(
        "Chromium/Chrome/Brave not found; install it or set MUSE2API_CHROMIUM_PATH"
    )


class ChromiumProcess:
    def __init__(self, executable: str, port: int, profile_dir: Path | None,
                 headless: bool = True, proxy: str = "", *, attach: bool = False,
                 host: str = "127.0.0.1", launch_cmd: str = "", launch_wait: float = 45.0):
        self.executable = executable
        self.port = port
        self.host = host
        self.profile_dir = profile_dir
        self.attach = attach
        # The user's own browser is never headless.
        self.headless = headless and not attach
        self.proxy = proxy
        self.launch_cmd = launch_cmd
        self.launch_wait = launch_wait
        self.proc: asyncio.subprocess.Process | None = None

    @property
    def http_base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def page_ws_url(self, target_id: str) -> str:
        return f"ws://{self.host}:{self.port}/devtools/page/{target_id}"

    async def browser_ws_url(self) -> str | None:
        try:
            # Local DevTools port must not follow http_proxy/https_proxy.
            async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                r = await client.get(f"{self.http_base}/json/version")
                url = r.json().get("webSocketDebuggerUrl")
        except (httpx.HTTPError, ValueError):
            return None
        if not url:
            return None
        # Behind a tunnel or port forward the browser reports its own port; talk to ours.
        path = url.split("/devtools/", 1)[-1]
        return f"ws://{self.host}:{self.port}/devtools/{path}"

    def launch_args(self) -> list[str]:
        if self.attach:
            # The user's real browser: no throwaway flags, normal profile unless set.
            args = [self.executable, f"--remote-debugging-port={self.port}",
                    "--no-first-run", "--no-default-browser-check"]
            if self.profile_dir:
                args.append(f"--user-data-dir={self.profile_dir}")
            if self.proxy:
                args.append(f"--proxy-server={self.proxy}")
            return args
        assert self.profile_dir is not None
        args = [
            self.executable,
            f"--remote-debugging-port={self.port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            "--disable-background-networking",
            "--autoplay-policy=no-user-gesture-required",
            "--window-size=1440,2200",
            "about:blank",
        ]
        if self.proxy:
            args.insert(1, f"--proxy-server={self.proxy}")
        if self.headless:
            args[1:1] = ["--headless=new", "--disable-gpu"]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            args.insert(1, "--no-sandbox")
        return args

    async def start(self) -> str:
        """Attach to a browser already exposing the DevTools port, or start one;
        return the browser ws URL."""
        if url := await self.browser_ws_url():
            log.info("attached to existing browser on %s", self.http_base)
            return url
        if self.attach:
            await self._launch_detached()
        else:
            await self._launch_owned()
        for _ in range(int(self.launch_wait / 0.5)):
            if url := await self.browser_ws_url():
                return url
            if self.proc and self.proc.returncode is not None:
                break
            await asyncio.sleep(0.5)
        if self.attach:
            raise RuntimeError(
                f"browser did not expose DevTools on {self.http_base}. If it is already "
                "running without --remote-debugging-port, quit it fully once so it can be "
                "started with the flag."
            )
        raise RuntimeError("Chromium did not expose a DevTools endpoint")

    async def _launch_owned(self) -> None:
        assert self.profile_dir is not None
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            with contextlib.suppress(FileNotFoundError):
                (self.profile_dir / lock).unlink()
        log.info("launching Chromium: %s", self.executable)
        self.proc = await asyncio.create_subprocess_exec(
            *self.launch_args(), stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _launch_detached(self) -> None:
        if self.launch_cmd:
            log.info("browser not reachable; running launch command: %s", self.launch_cmd)
            proc = await asyncio.create_subprocess_shell(
                self.launch_cmd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), 30)
            except asyncio.TimeoutError:
                proc.kill()
                raise RuntimeError("browser launch command timed out") from None
            if proc.returncode:
                text = out.decode(errors="replace").strip()[-300:]
                raise RuntimeError(f"browser launch command failed ({proc.returncode}): {text}")
            return
        args = self.launch_args()
        log.info("browser not reachable; starting %s", " ".join(args))
        kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                        "stderr": subprocess.DEVNULL, "close_fds": True}
        if sys.platform == "win32":
            kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                       | subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            kwargs["start_new_session"] = True
        # Not awaited and not kept: the browser belongs to the user, not to muse2api.
        subprocess.Popen(args, **kwargs)  # noqa: ASYNC220, S603

    async def stop(self) -> None:
        if self.attach:
            return  # never close the user's browser
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None
