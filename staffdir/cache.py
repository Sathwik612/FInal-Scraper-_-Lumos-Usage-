"""
staffdir.cache
==============

Optional SQLite-backed HTTP response cache. Value proposition:

  * Development iteration on the parser is 10-50x faster because
    identical URLs are served from disk.
  * Long crawls that get interrupted can be resumed without
    re-hammering the origin.
  * ETag / Last-Modified are stored so subsequent requests can be
    conditional (200 -> refresh; 304 -> return cached body).

Usage
-----
    from staffdir.cache import CachedFetcher
    fetcher = CachedFetcher("cache.sqlite")
    html = fetcher.get("https://school.edu/staff")

`CachedFetcher` is a thin wrapper over `requests.Session`; if you're
already using the legacy `master_staff_directory_scraper.Fetcher`,
you can attach the cache separately via `HttpCache.get(url)` and
`HttpCache.put(url, response)`.

Concurrency: SQLite is opened with WAL mode so multiple threads can
read while one writes. For very high concurrency (>32 workers)
consider the on-disk cache dir mode instead — but for this workload
(school-scraping), SQLite is fine.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

try:
    import requests                          # type: ignore
except Exception:                             # pragma: no cover
    requests = None

DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 7        # 1 week


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

@dataclass
class CachedEntry:
    url: str
    status: int
    body: str
    etag: str = ""
    last_modified: str = ""
    fetched_at: float = 0.0


class HttpCache:
    """SQLite key-value store, keyed by SHA-256 of the URL."""

    def __init__(self, db_path: str = "staffdir_cache.sqlite",
                 ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.db_path = db_path
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".",
                    exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pages(
                    key           TEXT PRIMARY KEY,
                    url           TEXT NOT NULL,
                    status        INTEGER NOT NULL,
                    body          TEXT NOT NULL,
                    etag          TEXT DEFAULT '',
                    last_modified TEXT DEFAULT '',
                    fetched_at    REAL NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pages_fetched ON pages(fetched_at)")

    def _connect(self):
        return sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)

    @staticmethod
    def _key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def get(self, url: str) -> Optional[CachedEntry]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT url,status,body,etag,last_modified,fetched_at "
                "FROM pages WHERE key = ?", (self._key(url),)).fetchone()
        if not row: return None
        entry = CachedEntry(*row)
        if self.ttl and time.time() - entry.fetched_at > self.ttl:
            return None
        return entry

    def put(self, url: str, status: int, body: str,
            etag: str = "", last_modified: str = "") -> None:
        with self._lock, self._connect() as conn:
            conn.execute("""
                INSERT INTO pages(key,url,status,body,etag,last_modified,fetched_at)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET
                    status=excluded.status, body=excluded.body,
                    etag=excluded.etag, last_modified=excluded.last_modified,
                    fetched_at=excluded.fetched_at
            """, (self._key(url), url, status, body, etag, last_modified,
                  time.time()))
            conn.commit()

    def delete(self, url: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM pages WHERE key = ?", (self._key(url),))
            conn.commit()

    def stats(self) -> dict:
        with self._connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            oldest, newest = conn.execute(
                "SELECT MIN(fetched_at), MAX(fetched_at) FROM pages"
            ).fetchone()
        return {"entries": n, "oldest": oldest, "newest": newest,
                "path": self.db_path}


# ---------------------------------------------------------------------------
# Cached fetcher (drop-in for a requests Session for simple GETs)
# ---------------------------------------------------------------------------

class CachedFetcher:
    """Minimal HTTP GET with cache + conditional requests. Use the
    legacy Fetcher for full retries/rate-limit/robots; use this when
    you just want fast repeatable dev fetches."""

    def __init__(self, cache_path: str = "staffdir_cache.sqlite",
                 user_agent: str = "staffdir/1.0",
                 timeout: int = 20, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        if requests is None:
            raise RuntimeError("requests must be installed")
        self.cache = HttpCache(cache_path, ttl_seconds=ttl_seconds)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent,
                                     "Accept": "text/html,*/*;q=0.8"})
        self.timeout = timeout

    def get(self, url: str, *, force_refresh: bool = False) -> Optional[str]:
        if not force_refresh:
            hit = self.cache.get(url)
            if hit and hit.status < 400:
                return hit.body
        headers = {}
        stale = self.cache.get(url) if not force_refresh else None
        if stale and stale.etag:
            headers["If-None-Match"] = stale.etag
        if stale and stale.last_modified:
            headers["If-Modified-Since"] = stale.last_modified
        try:
            r = self.session.get(url, headers=headers, timeout=self.timeout,
                                 allow_redirects=True)
        except Exception:
            return stale.body if stale else None
        if r.status_code == 304 and stale:
            return stale.body
        if r.status_code >= 400:
            return stale.body if stale else None
        self.cache.put(url, r.status_code, r.text,
                       etag=r.headers.get("ETag", ""),
                       last_modified=r.headers.get("Last-Modified", ""))
        return r.text
