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


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else v.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- environment
# Vercel sets VERCEL=1 in builds and functions. SERVERLESS=1 opts any other read-only,
# short-lived platform (AWS Lambda, Cloud Run jobs, ...) into the same behaviour.
ON_VERCEL = os.getenv("VERCEL") == "1"
SERVERLESS = ON_VERCEL or _bool("SERVERLESS", False)

# Where runtime files may be written. The application directory is never written to on
# serverless platforms (it is read-only there); /tmp is writable but ephemeral and
# per-instance, so it only holds scratch data and the explicit-opt-in demo database.
RUNTIME_DIR = Path(os.getenv("RUNTIME_DIR") or ("/tmp/distill" if SERVERLESS else str(BASE_DIR / "data")))


def normalize_database_url(url: str) -> str:
    """Hosted Postgres providers (Neon, Supabase, Vercel Marketplace, Render, ...) hand out
    postgres:// or postgresql:// URLs. SQLAlchemy needs the driver named: we ship psycopg 3."""
    url = url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _database_url() -> tuple[str, bool]:
    """(url, explicitly_configured). Vercel's Postgres integrations set POSTGRES_URL or
    DATABASE_URL; DATABASE_URL wins when both exist."""
    for name in ("DATABASE_URL", "POSTGRES_URL", "POSTGRES_PRISMA_URL"):
        v = os.getenv(name, "").strip()
        if v:
            return normalize_database_url(v), True
    return f"sqlite:///{RUNTIME_DIR / 'app.db'}", False


_DB_URL, _DB_EXPLICIT = _database_url()


@dataclass
class Settings:
    # environment
    on_vercel: bool = ON_VERCEL
    serverless: bool = SERVERLESS
    runtime_dir: Path = RUNTIME_DIR

    # storage
    database_url: str = _DB_URL
    database_explicit: bool = _DB_EXPLICIT
    # "local": files on disk under storage_dir (development, Docker with a volume)
    # "database": files stored in the database (persistent on serverless, no extra service)
    storage_backend: str = (os.getenv("STORAGE_BACKEND") or ("database" if SERVERLESS else "local")).lower()
    storage_dir: Path = Path(os.getenv("STORAGE_DIR") or str(RUNTIME_DIR / "files"))
    max_stored_file_bytes: int = _int("MAX_STORED_FILE_BYTES", 25 * 1024 * 1024)
    # Running on a serverless platform with an ephemeral SQLite file must be a deliberate choice
    allow_ephemeral_storage: bool = _bool("ALLOW_EPHEMERAL_STORAGE", False)

    # jobs: "background" runs scrapes/generations after the response (long-lived servers);
    # "inline" finishes them inside the request (serverless, where the instance may be frozen
    # as soon as a response is sent)
    job_mode: str = (os.getenv("JOB_MODE") or ("inline" if SERVERLESS else "background")).lower()
    # a job still "running" after this long is treated as interrupted (crash, timeout, freeze)
    stale_job_minutes: float = _float("STALE_JOB_MINUTES", 6 if SERVERLESS else 30)

    # fetching
    user_agent: str = os.getenv(
        "USER_AGENT", "Mozilla/5.0 (compatible; Distill/2.0; +https://github.com/distill-scraper)"
    )
    request_timeout: float = _float("REQUEST_TIMEOUT", 20)
    max_page_bytes: int = _int("MAX_PAGE_BYTES", 8 * 1024 * 1024)
    max_redirects: int = _int("MAX_REDIRECTS", 5)
    # Chromium isn't available in Vercel functions; static fetching and the ReportLab PDF
    # renderer are used instead unless a browser is explicitly enabled
    js_rendering: bool = _bool("JS_RENDERING", not SERVERLESS)
    browser_channel: str | None = os.getenv("BROWSER_CHANNEL") or None
    take_screenshots: bool = _bool("TAKE_SCREENSHOTS", not SERVERLESS)

    # crawl limits (hard caps, regardless of what a request asks for). Serverless requests must
    # finish within the function's max duration, so the defaults are smaller there.
    max_pages_cap: int = _int("MAX_PAGES_CAP", 20 if SERVERLESS else 100)
    max_depth_cap: int = _int("MAX_DEPTH_CAP", 3 if SERVERLESS else 5)
    min_delay: float = _float("MIN_CRAWL_DELAY", 0.5)

    # SSRF: host:port pairs that may resolve to private addresses (e.g. the local demo site)
    private_host_allowlist: list[str] = field(default_factory=lambda: _list("PRIVATE_HOST_ALLOWLIST"))

    # API protection (in-memory: per process/instance)
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

    @property
    def database_is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def database_persistent(self) -> bool:
        """False when data would silently disappear (SQLite on a serverless platform)."""
        return not (self.serverless and self.database_is_sqlite)

    def deployment_problems(self) -> list[str]:
        """Configuration errors that make the deployment unusable. Empty means ready."""
        problems = []
        if not self.database_persistent and not self.allow_ephemeral_storage:
            problems.append(
                "No persistent database is configured. On Vercel the filesystem is read-only and /tmp is "
                "wiped between instances, so set DATABASE_URL to a hosted PostgreSQL database (Neon, Supabase, "
                "Vercel Marketplace Postgres...). To run a throwaway demo on temporary storage instead, set "
                "ALLOW_EPHEMERAL_STORAGE=1.")
        if self.storage_backend not in ("local", "database"):
            problems.append(f"STORAGE_BACKEND must be 'local' or 'database' (got '{self.storage_backend}').")
        if self.serverless and self.storage_backend == "local" and not self.allow_ephemeral_storage:
            problems.append("STORAGE_BACKEND=local keeps generated files in /tmp, which is lost between serverless "
                            "invocations. Use STORAGE_BACKEND=database (the default on Vercel).")
        if self.job_mode not in ("inline", "background"):
            problems.append(f"JOB_MODE must be 'inline' or 'background' (got '{self.job_mode}').")
        return problems

    def deployment_warnings(self) -> list[str]:
        warnings = []
        if self.serverless and self.allow_ephemeral_storage and not self.database_persistent:
            warnings.append("Running on temporary storage (ALLOW_EPHEMERAL_STORAGE=1): projects and files can "
                            "disappear at any time.")
        if self.serverless and self.job_mode == "background":
            warnings.append("JOB_MODE=background on a serverless platform: jobs may freeze after the response.")
        return warnings


def ensure_dir(path: Path) -> Path:
    """Create a runtime directory on first use (never at import time)."""
    path.mkdir(parents=True, exist_ok=True)
    return path


settings = Settings()
