"""File storage for generated outputs and screenshots.

Files are addressed by a key such as ``<project_id>/outputs/<output_id>.pdf`` and stored in
one of two backends, chosen by STORAGE_BACKEND:

* ``local`` (default for development and Docker): files on disk under STORAGE_DIR.
* ``database`` (default on Vercel/serverless): file bytes in the ``stored_files`` table of the
  configured database, so every instance can serve them and they survive cold starts.

Large deployments can add an object-store backend (S3, R2, Vercel Blob) by implementing the
same four methods; nothing else in the app touches the filesystem for these files.
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import ensure_dir, settings

_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,500}$")


class StorageError(Exception):
    pass


def _check_key(key: str) -> str:
    if not _KEY_RE.match(key) or ".." in key.split("/") or key.startswith("/"):
        raise StorageError(f"invalid storage key: {key!r}")
    return key


class LocalStorage:
    name = "local"

    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path | None:
        # legacy rows stored absolute file paths; accept them only inside the storage root
        if key.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", key):
            p = Path(key)
        else:
            p = self.root / _check_key(key)
        try:
            resolved = p.resolve()
            root = self.root.resolve()
        except OSError:
            return None
        return resolved if resolved.is_relative_to(root) else None

    def put(self, key: str, data: bytes, mime: str = "application/octet-stream") -> str:
        if len(data) > settings.max_stored_file_bytes:
            raise StorageError(f"file is larger than the {settings.max_stored_file_bytes // 1_000_000} MB limit")
        p = self.root / _check_key(key)
        ensure_dir(p.parent)
        tmp = p.with_suffix(p.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(p)          # atomic on the same filesystem
        return key

    def get(self, key: str) -> bytes | None:
        p = self._path(key)
        return p.read_bytes() if p and p.is_file() else None

    def exists(self, key: str) -> bool:
        p = self._path(key)
        return bool(p and p.is_file())

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p and p.is_file():
            p.unlink()

    def delete_prefix(self, prefix: str) -> None:
        folder = self.root / _check_key(prefix.rstrip("/"))
        if not folder.exists() or not folder.resolve().is_relative_to(self.root.resolve()):
            return
        for f in sorted(folder.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
        folder.rmdir()


class DatabaseStorage:
    name = "database"

    def put(self, key: str, data: bytes, mime: str = "application/octet-stream") -> str:
        from .db import StoredFile, ensure_db, session
        if len(data) > settings.max_stored_file_bytes:
            raise StorageError(f"file is larger than the {settings.max_stored_file_bytes // 1_000_000} MB limit")
        ensure_db()
        _check_key(key)
        with session() as s:
            row = s.get(StoredFile, key)
            if row:
                row.content, row.mime, row.size = data, mime, len(data)
            else:
                s.add(StoredFile(key=key, content=data, mime=mime, size=len(data)))
        return key

    def get(self, key: str) -> bytes | None:
        from .db import StoredFile, ensure_db, session
        ensure_db()
        with session() as s:
            row = s.get(StoredFile, key)
            return bytes(row.content) if row else None

    def exists(self, key: str) -> bool:
        from .db import StoredFile, ensure_db, session
        ensure_db()
        with session() as s:
            return s.query(StoredFile.key).filter_by(key=key).first() is not None

    def delete(self, key: str) -> None:
        from .db import StoredFile, ensure_db, session
        ensure_db()
        with session() as s:
            s.query(StoredFile).filter_by(key=key).delete()

    def delete_prefix(self, prefix: str) -> None:
        from .db import StoredFile, ensure_db, session
        ensure_db()
        prefix = _check_key(prefix.rstrip("/")) + "/"
        with session() as s:
            s.query(StoredFile).filter(StoredFile.key.startswith(prefix, autoescape=True)).delete(
                synchronize_session=False)


_storage: LocalStorage | DatabaseStorage | None = None


def get_storage() -> LocalStorage | DatabaseStorage:
    global _storage
    if _storage is None:
        _storage = DatabaseStorage() if settings.storage_backend == "database" else LocalStorage(settings.storage_dir)
    return _storage


def reset_storage() -> None:
    """For tests that switch backends."""
    global _storage
    _storage = None
