"""Runtime configuration, loaded from environment variables (prefix ``MUSE2API_``) and ``.env``."""

from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DriverName = Literal["mock", "browser", "http"]
PoolStrategyName = Literal["round_robin", "lru", "affinity"]
BrowserMode = Literal["launch", "attach"]
TabMode = Literal["window", "minimized", "background", "hidden"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MUSE2API_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- server ---
    host: str = "127.0.0.1"
    port: int = 18610
    log_level: str = "INFO"
    public_base: str = Field(
        default="",
        description="Externally reachable base URL used when building media links. "
        "Empty means derive from the incoming request.",
    )

    # --- auth ---
    api_key: str = Field(default="", description="Bearer key for /v1/*. Auto-generated if empty.")
    admin_key: str = Field(default="", description="Bearer key for /admin/*. Falls back to api_key.")

    # --- storage ---
    data_dir: Path = Path("data")
    request_log_retention_days: float = Field(
        default=14, description="Days of /v1/* request history kept in data/requests.db."
    )
    media_retention_days: float = Field(
        default=14, description="Days generated media files are kept in data/media. "
        "0 disables automatic cleanup."
    )

    # --- upstream driver ---
    driver: DriverName = "mock"
    site_url: str = "https://muse.ai"

    # browser driver
    browser_mode: BrowserMode = Field(
        default="launch",
        description="launch: run a dedicated (headless) Chromium owned by muse2api. "
        "attach: drive the user's own browser (e.g. Brave) over its DevTools port, "
        "starting it when it is not running and never closing it.",
    )
    chromium_path: str = ""
    cdp_host: str = Field(
        default="127.0.0.1",
        description="Host of the DevTools endpoint (attach mode can point at a tunnel).",
    )
    cdp_port: int = 19210
    headless: bool = True
    browser_launch_cmd: str = Field(
        default="",
        description="attach mode: shell command that starts the browser when the DevTools "
        "port does not answer. Empty = run MUSE2API_CHROMIUM_PATH with "
        "--remote-debugging-port.",
    )
    browser_launch_wait: float = 45.0
    tab_mode: TabMode = Field(
        default="window",
        description="How request tabs are opened. window: a new window per tab (old "
        "behaviour, fine headless). minimized: a new background window per tab, created "
        "minimized - never shows up or takes focus, still renders and is not throttled "
        "(recommended for attach). background: a background tab in an existing window. "
        "hidden: windowless target - invisible, but Chromium never paints it, so muse.ai "
        "does not finish hydrating; kept for experiments only.",
    )
    shared_profile: bool = Field(
        default=False,
        description="attach mode: use the browser profile's own muse.ai login (default "
        "browser context) instead of one isolated context per account with imported "
        "cookies. An account named 'browser' is created/synced from the profile cookies.",
    )
    cookie_source_cdp: str = Field(
        default="",
        description="DevTools endpoint (host:port) of an everyday browser, e.g. Brave, whose "
        "muse.ai login the headless browser borrows. The account 'browser' is synced "
        "from that profile's cookies at startup and on every keepalive, and rotated "
        "cookies are written back. Nothing is opened in that browser.",
    )
    cookie_source_launch_cmd: str = Field(
        default="",
        description="Shell command that starts the cookie source browser when its "
        "DevTools port does not answer.",
    )
    browser_proxy: str = Field(
        default="",
        description="Proxy for the browser only, e.g. socks5://127.0.0.1:10808 or http://10.144.1.10:8080. "
        "Python's own calls (session renewal) follow http_proxy/https_proxy instead.",
    )
    browser_profile_dir: Path | None = None
    page_ready_timeout: float = 45.0
    warm_threads: int = Field(
        default=2,
        description="Pre-started muse.ai threads kept per account. A new thread takes muse.ai "
        "20-40s to answer its first message, a started one about 5s, so a chat that opens a new "
        "thread goes into a warm one instead. Each costs one extra short message of quota. "
        "Needs MUSE2API_ACCOUNT_MAX_CONCURRENCY of at least 2; 0 disables.",
    )
    warm_prompt: str = Field(default="Hi", description="Message that starts a warm thread.")

    # --- timeouts (seconds) ---
    chat_timeout: float = 300.0
    first_token_timeout: float = 75.0
    stream_heartbeat_interval: float = Field(
        default=0.0,
        description="SSE ': keepalive' comment interval while waiting for the first "
        "stream token; 0 disables. Keeps idle clients/proxies from timing out.",
    )
    image_timeout: float = 240.0
    video_timeout: float = 600.0

    # --- account pool ---
    pool_strategy: PoolStrategyName = "affinity"
    pool_acquire_timeout: float = 60.0
    account_max_concurrency: int = 4
    account_cooldown: float = 120.0
    max_failover: int = 2

    # --- background removal (images with background="transparent") ---
    matting_model: str = Field(
        default="birefnet-general",
        description="rembg model used to cut out the subject, e.g. birefnet-general "
        "(best) or birefnet-general-lite (faster).",
    )

    # --- keepalive (session renewal) ---
    keepalive_enabled: bool = True
    keepalive_interval: float = 600

    @field_validator("browser_profile_dir", mode="before")
    @classmethod
    def _blank_profile_dir_is_unset(cls, value: object) -> object:
        """``.env.example`` ships ``MUSE2API_BROWSER_PROFILE_DIR=``; ``Path("")`` would be the
        working directory, so Chrome would scatter its profile across the repo root."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def accounts_file(self) -> Path:
        return self.data_dir / "accounts.json"

    @property
    def tasks_file(self) -> Path:
        return self.data_dir / "tasks.json"

    @property
    def key_file(self) -> Path:
        return self.data_dir / "api_key"

    @property
    def keys_file(self) -> Path:
        return self.data_dir / "keys.json"

    @property
    def requests_db(self) -> Path:
        return self.data_dir / "requests.db"

    @property
    def profile_dir(self) -> Path | None:
        """Launch mode always has a profile dir; attach mode uses the browser's normal
        profile unless one is configured."""
        if self.browser_mode == "attach":
            return self.browser_profile_dir
        return self.browser_profile_dir or (self.data_dir / "browser-profile")

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.media_dir):
            d.mkdir(parents=True, exist_ok=True)

    def resolve_api_key(self) -> str:
        """Return the configured key, or load/generate a persistent one under ``data_dir``."""
        if self.api_key:
            return self.api_key
        self.ensure_dirs()
        if self.key_file.is_file():
            key = self.key_file.read_text(encoding="utf-8").strip()
            if key:
                self.api_key = key
                return key
        key = "m2a-" + secrets.token_urlsafe(24)
        self.key_file.write_text(key + "\n", encoding="utf-8")
        self.key_file.chmod(0o600)
        self.api_key = key
        return key

    @property
    def effective_admin_key(self) -> str:
        return self.admin_key or self.api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
