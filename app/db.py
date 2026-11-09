"""Database models (SQLAlchemy 2). SQLite for local development, PostgreSQL via DATABASE_URL.

Nothing here touches the filesystem at import time: the engine connects lazily and
`ensure_db()` creates the SQLite directory (if any) and the tables on first use."""
from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text, create_engine, event
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import ensure_dir, settings


def _id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    source_url: Mapped[str] = mapped_column(Text)
    config: Mapped[dict] = mapped_column(JSON, default=dict)      # the scrape request
    scraping_status: Mapped[str] = mapped_column(String(20), default="queued")
    site_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    screenshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    pages: Mapped[list["ScrapedPage"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="ScrapedPage.position")
    outputs: Mapped[list["GeneratedOutput"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="GeneratedOutput.created_at.desc()")
    jobs: Mapped[list["ScrapingJob"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="ScrapingJob.started_at.desc()")


class ScrapedPage(Base):
    __tablename__ = "scraped_pages"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="")
    extracted_content: Mapped[dict] = mapped_column(JSON)   # the standard document (see scraping/document.py)
    page_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    project: Mapped[Project] = relationship(back_populates="pages")


class ScrapingJob(Base):
    __tablename__ = "scraping_jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|done|failed|cancelled
    stage: Mapped[str] = mapped_column(String(40), default="queued")
    progress: Mapped[float] = mapped_column(Float, default=0.0)        # 0..1
    detail: Mapped[str] = mapped_column(Text, default="")
    page_errors: Mapped[list] = mapped_column(JSON, default=list)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    project: Mapped[Project] = relationship(back_populates="jobs")


class GeneratedOutput(Base):
    __tablename__ = "generated_outputs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))          # convert | ai
    format: Mapped[str] = mapped_column(String(40))        # file format (convert) or ai output type
    configuration: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|done|failed
    stage: Mapped[str] = mapped_column(String(80), default="")
    generated_content: Mapped[str | None] = mapped_column(Text, nullable=True)  # editable Markdown / text
    title: Mapped[str] = mapped_column(Text, default="")
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    project: Mapped[Project] = relationship(back_populates="outputs")


class ChatSession(Base):
    """A conversation with the extraction assistant. `messages` is the provider-neutral
    transcript (user / assistant / tool); tool messages also carry a `display` card for the UI."""
    __tablename__ = "chat_sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(Text, default="New chat")
    messages: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="idle")   # idle | running
    status_detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class StoredFile(Base):
    """Generated files and screenshots when STORAGE_BACKEND=database (the serverless default).
    Keeps files next to the data that references them, so any instance can serve them."""
    __tablename__ = "stored_files"
    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    mime: Mapped[str] = mapped_column(String(200), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


def _make_engine():  # noqa: ANN202
    url = settings.database_url
    if url.startswith("sqlite"):
        eng = create_engine(url, connect_args={"check_same_thread": False}, pool_pre_ping=True)

        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()
        return eng
    # prepare_threshold=None: psycopg 3 otherwise creates server-side prepared statements, which
    # break behind transaction-mode poolers (PgBouncer, Neon "-pooler", Supabase pooler) with
    # "prepared statement _pg3_0 already exists". Those poolers are the norm for serverless.
    pg_args = {"connect_timeout": 10, "prepare_threshold": None}
    if settings.serverless:
        # Instances are frozen between requests, so pooled connections go stale. Open a
        # connection per unit of work and let the provider's pooler do the pooling.
        return create_engine(url, poolclass=NullPool, connect_args=pg_args)
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5, pool_recycle=1800,
                         connect_args=pg_args)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

LOCAL_USER_EMAIL = "local@distill"
_init_lock = threading.Lock()
_initialized = False


def _sqlite_path() -> Path | None:
    url = settings.database_url
    if not url.startswith("sqlite:///") or url in ("sqlite://", "sqlite:///:memory:"):
        return None
    return Path(url[len("sqlite:///"):])


def ensure_db() -> None:
    """Idempotent: create the SQLite directory, tables and the local user on first use."""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        path = _sqlite_path()
        if path is not None:
            ensure_dir(path.parent)
        try:
            Base.metadata.create_all(engine)
        except (OperationalError, ProgrammingError):
            # another instance created the tables at the same moment; retry once
            Base.metadata.create_all(engine)
        try:
            with session() as s:
                if not s.query(User).filter_by(email=LOCAL_USER_EMAIL).first():
                    s.add(User(email=LOCAL_USER_EMAIL, name="Local user"))
        except IntegrityError:
            pass  # created concurrently by another instance
        _initialized = True


init_db = ensure_db  # backwards-compatible name


@contextmanager
def session() -> Iterator:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def local_user_id() -> str:
    with session() as s:
        return s.query(User).filter_by(email=LOCAL_USER_EMAIL).one().id
