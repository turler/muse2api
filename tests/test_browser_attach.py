"""Attach mode (drive the user's own Brave/Chrome) and silent tab modes."""

from __future__ import annotations

import pytest

from muse2api.accounts import Account
from muse2api.accounts.pool import AccountPool
from muse2api.accounts.store import JsonAccountStore
from muse2api.config import Settings
from muse2api.drivers.browser.chromium import ChromiumProcess, find_chromium
from muse2api.drivers.browser.driver import BrowserDriver


def _drv(tmp_path, **kw) -> BrowserDriver:
    return BrowserDriver(Settings(_env_file=None, data_dir=tmp_path, **kw))


@pytest.mark.parametrize("mode,attach,expected", [
    ("hidden", True, {"hidden": True}),
    ("background", True, {"background": True}),
    ("window", False, {"newWindow": True}),
    ("window", True, {"newWindow": True, "background": True}),
])
def test_target_params_per_tab_mode(tmp_path, mode, attach, expected):
    drv = _drv(tmp_path, tab_mode=mode, browser_mode="attach" if attach else "launch")
    params = drv._target_params("ctx1")
    assert params == {"url": "about:blank", "browserContextId": "ctx1", **expected}


def test_shared_profile_uses_the_default_context(tmp_path):
    drv = _drv(tmp_path, tab_mode="hidden", browser_mode="attach", shared_profile=True)
    assert "browserContextId" not in drv._target_params("")


async def test_shared_profile_context_is_default(tmp_path):
    drv = _drv(tmp_path, browser_mode="attach", shared_profile=True)
    assert await drv._context(Account(id="a")) == ""


def test_attach_mode_has_no_default_profile_dir(tmp_path):
    assert Settings(_env_file=None, data_dir=tmp_path, browser_mode="attach").profile_dir is None
    assert Settings(_env_file=None, data_dir=tmp_path).profile_dir == tmp_path / "browser-profile"


def test_attach_launch_args_are_not_headless_and_keep_the_user_profile():
    proc = ChromiumProcess("brave.exe", 9222, None, headless=True, attach=True)
    args = proc.launch_args()
    assert args[:2] == ["brave.exe", "--remote-debugging-port=9222"]
    assert not any(a.startswith(("--headless", "--user-data-dir", "--window-size")) for a in args)


def test_launch_mode_args_unchanged(tmp_path):
    args = ChromiumProcess("chromium", 19210, tmp_path, headless=True).launch_args()
    assert "--headless=new" in args and f"--user-data-dir={tmp_path}" in args


async def test_attach_stop_never_kills_the_browser():
    proc = ChromiumProcess("brave", 9222, None, attach=True)

    class Boom:
        returncode = None

        def terminate(self):
            raise AssertionError("must not terminate the user's browser")

    proc.proc = Boom()  # type: ignore[assignment]
    await proc.stop()


async def test_ws_url_is_rewritten_to_the_configured_endpoint(monkeypatch):
    proc = ChromiumProcess("brave", 19222, None, attach=True, host="10.0.0.5")

    class Resp:
        def json(self):
            return {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc"}

    class Client:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            assert url == "http://10.0.0.5:19222/json/version"
            return Resp()

    monkeypatch.setattr("muse2api.drivers.browser.chromium.httpx.AsyncClient", Client)
    assert await proc.browser_ws_url() == "ws://10.0.0.5:19222/devtools/browser/abc"
    assert proc.page_ws_url("T1") == "ws://10.0.0.5:19222/devtools/page/T1"


async def test_attach_runs_the_launch_command_when_nothing_answers(monkeypatch, tmp_path):
    marker = tmp_path / "started"
    proc = ChromiumProcess("", 9222, None, attach=True, launch_cmd=f"touch {marker}",
                           launch_wait=1)
    answers = iter([None, "ws://127.0.0.1:9222/devtools/browser/x"])

    async def fake_url():
        return next(answers, "ws://127.0.0.1:9222/devtools/browser/x")

    monkeypatch.setattr(proc, "browser_ws_url", fake_url)
    assert (await proc.start()).endswith("/browser/x")
    assert marker.exists()


async def test_failing_launch_command_is_reported(monkeypatch):
    proc = ChromiumProcess("", 9222, None, attach=True, launch_cmd="exit 3", launch_wait=1)

    async def none():
        return None

    monkeypatch.setattr(proc, "browser_ws_url", none)
    with pytest.raises(RuntimeError, match="launch command failed"):
        await proc.start()


def test_find_chromium_prefers_brave_in_attach_mode(monkeypatch):
    monkeypatch.setattr("muse2api.drivers.browser.chromium.shutil.which",
                        lambda c: c if c in ("brave-browser", "chromium") else None)
    assert find_chromium(prefer_brave=True) == "brave-browser"
    assert find_chromium() == "chromium"


async def test_profile_login_is_mirrored_into_the_browser_account(tmp_path, monkeypatch):
    drv = _drv(tmp_path, browser_mode="attach", shared_profile=True)

    async def cookies():
        return {"hatch_sess": "s", "hatch_native_auth_device": "d"}, {"hatch_sess": 99}

    monkeypatch.setattr(drv, "profile_cookies", cookies)
    pool = AccountPool(JsonAccountStore(tmp_path / "accounts.json"))
    acc = await drv.sync_profile_account(pool)
    assert acc.id == "browser" and pool.get("browser").cookies["hatch_sess"] == "s"
    assert acc.cookie_expires == {"hatch_sess": 99}
