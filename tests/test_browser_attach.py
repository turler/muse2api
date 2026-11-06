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
    ("minimized", True, {"newWindow": True, "background": True, "windowState": "minimized"}),
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
    drv = _drv(tmp_path, tab_mode="minimized", browser_mode="attach", shared_profile=True)
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


# ---- headless browser borrowing the login of a cookie source browser (Brave) ----

from muse2api.drivers.base import SessionInfo  # noqa: E402
from muse2api.drivers.browser.cookies import CookieSource, muse_cookies  # noqa: E402


def test_muse_cookies_keeps_only_muse_domains():
    raw = [
        {"name": "hatch_sess", "value": "s", "domain": ".muse.ai", "expires": 123.9},
        {"name": "x", "value": "1", "domain": "app.muse.ai", "expires": -1},
        {"name": "hatch_sess", "value": "evil", "domain": "notmuse.ai"},
        {"name": "empty", "value": "", "domain": "muse.ai"},
    ]
    assert muse_cookies(raw) == ({"hatch_sess": "s", "x": "1"}, {"hatch_sess": 123})


class _FakeSource(CookieSource):
    def __init__(self, jar):
        super().__init__("127.0.0.1:9")
        self.jar, self.written = jar, []

    async def get_cookies(self):
        return [{"name": k, "value": v, "domain": ".muse.ai"} for k, v in self.jar.items()]

    async def set_cookies(self, cookies):
        self.written.extend(cookies)


def _headless(tmp_path, jar):
    drv = _drv(tmp_path, cookie_source_cdp="127.0.0.1:9")
    drv._cookie_source = _FakeSource(jar)
    return drv


async def test_headless_driver_syncs_the_account_from_the_cookie_source(tmp_path):
    drv = _headless(tmp_path, {"hatch_sess": "s1", "hatch_native_auth_device": "d"})
    pool = AccountPool(JsonAccountStore(tmp_path / "accounts.json"))
    acc = await drv.sync_profile_account(pool)
    assert acc is not None and acc.cookies["hatch_sess"] == "s1"
    # Isolated context + imported cookies: the headless browser keeps its own jar.
    assert drv._target_params("ctx")["browserContextId"] == "ctx"


async def test_invalid_account_revives_only_on_a_new_profile_login(tmp_path):
    from muse2api.accounts.model import AccountStatus

    drv = _headless(tmp_path, {"hatch_sess": "s1", "hatch_native_auth_device": "d"})
    pool = AccountPool(JsonAccountStore(tmp_path / "accounts.json"))
    acc = await drv.sync_profile_account(pool)
    acc.status = AccountStatus.INVALID
    await drv.sync_profile_account(pool)
    assert pool.get("browser").status == AccountStatus.INVALID  # same dead session
    drv._cookie_source.jar["hatch_sess"] = "s2"  # user logged in again in Brave
    await drv.sync_profile_account(pool)
    assert pool.get("browser").status == AccountStatus.ACTIVE


async def test_renewal_uses_profile_cookies_and_writes_rotations_back(tmp_path, monkeypatch):
    drv = _headless(tmp_path, {"hatch_sess": "s1", "hatch_native_auth_device": "d",
                               "hatch_vml": "old"})
    seen = {}

    async def fake_renew(account):
        seen.update(account.cookies)
        return SessionInfo(ok=True, cookies={**account.cookies, "hatch_vml": "new"},
                           cookie_expires={"hatch_vml": 2_000_000_000})

    monkeypatch.setattr(drv, "_renew_isolated", fake_renew)
    stale = Account(id="browser", cookies={"hatch_sess": "stale"})
    info = await drv.renew_session(stale)
    assert seen["hatch_sess"] == "s1"  # the profile's current login, not the stale copy
    assert info.cookies["hatch_vml"] == "new"
    assert [c["name"] for c in drv._cookie_source.written] == ["hatch_vml"]


async def test_other_accounts_renew_as_before(tmp_path, monkeypatch):
    drv = _headless(tmp_path, {"hatch_sess": "s1", "hatch_native_auth_device": "d"})
    called = []

    async def fake_renew(account):
        called.append(account.id)
        return SessionInfo(ok=True)

    monkeypatch.setattr(drv, "_renew_isolated", fake_renew)
    await drv.renew_session(Account(id="acc_other", cookies={"hatch_sess": "x"}))
    assert called == ["acc_other"] and drv._cookie_source.written == []
