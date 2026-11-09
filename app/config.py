"""All settings come from environment variables (see .env.example)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env loader so the app runs without python-dotenv."""
    env = BASE_DIR / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if v[:1] in ('"', "'") and v[-1:] == v[:1]:
            v = v[1:-1]
        else:
            v = re.sub(r"\s+#.*$", "", v)          # inline comment
        os.environ.setdefault(k.strip(), v)


_load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _list(name: str) -> list[str]:
    return [x.strip().lower() for x in os.getenv(name, "").split(",") if x.strip()]


@dataclass
class Settings:
    # storage
    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'data' / 'app.db'}")
    storage_dir: Path = Path(os.getenv("STORAGE_DIR", str(BASE_DIR / "data" / "files")))

    # fetching
    user_agent: str = os.getenv(
        "USER_AGENT", "Mozilla/5.0 (compatible; Distill/2.0; +https://github.com/distill-scraper)"
    )
    request_timeout: float = _float("REQUEST_TIMEOUT", 20)
    max_page_bytes: int = _int("MAX_PAGE_BYTES", 8 * 1024 * 1024)
    max_redirects: int = _int("MAX_REDIRECTS", 5)
    js_rendering: bool = os.getenv("JS_RENDERING", "true").lower() == "true"
    browser_channel: str | None = os.getenv("BROWSER_CHANNEL") or None
    take_screenshots: bool = os.getenv("TAKE_SCREENSHOTS", "true").lower() == "true"

    # crawl limits (hard caps, regardless of what a request asks for)
    max_pages_cap: int = _int("MAX_PAGES_CAP", 100)
    max_depth_cap: int = _int("MAX_DEPTH_CAP", 5)
    min_delay: float = _float("MIN_CRAWL_DELAY", 0.5)

    # SSRF: host:port pairs that may resolve to private addresses (e.g. the local demo site)
    private_host_allowlist: list[str] = field(default_factory=lambda: _list("PRIVATE_HOST_ALLOWLIST"))

    # API protection
    rate_limit_per_minute: int = _int("RATE_LIMIT_PER_MINUTE", 30)
    max_concurrent_jobs: int = _int("MAX_CONCURRENT_JOBS", 3)

    # AI
    ai_provider: str = os.getenv("AI_PROVIDER", "anthropic").lower()   # anthropic | openai | gemini
    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY") or None
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    anthropic_fast_model: str = os.getenv("ANTHROPIC_FAST_MODEL", "claude-haiku-5-5")
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY") or None
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-5")
    openai_fast_model: str = os.getenv("OPENAI_FAST_MODEL", "gpt-5-mini")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    gemini_api_key: str | None = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or None
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    gemini_fast_model: str = os.getenv("GEMINI_FAST_MODEL", "gemini-3.5-flash-lite")
    gemini_base_url: str = os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
    ai_timeout: float = _float("AI_TIMEOUT", 120)
    # characters of page text per chunk (~4 chars per token)
    ai_chunk_chars: int = _int("AI_CHUNK_CHARS", 24000)
    ai_max_total_chars: int = _int("AI_MAX_TOTAL_CHARS", 400000)

    def ai_configured(self) -> bool:
        if self.ai_provider == "openai":
            return bool(self.openai_api_key)
        if self.ai_provider == "gemini":
            return bool(self.gemini_api_key)
        return bool(self.anthropic_api_key)


settings = Settings()
settings.storage_dir.mkdir(parents=True, exist_ok=True)
(BASE_DIR / "data").mkdir(exist_ok=True)
