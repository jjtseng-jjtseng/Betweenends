"""Polite, resumable, caching HTTP client for the Between Ends results API.

Design goals
------------
* **Polite** - one global rate limiter shared by every worker thread, so the
  request rate is bounded no matter how many workers are used.
* **Faithful** - the *exact* bytes the API returned are stored (gzip'd), so the
  raw layer is an untouched snapshot and normalisation can be re-run offline.
* **Resumable** - anything already on disk is never re-fetched (unless
  ``refresh=True``). Writes are atomic (tmp file + rename), so an interrupted
  run never leaves a truncated cache entry.
* **Robust** - retries with exponential backoff on network errors, 429/5xx
  (Heroku dynos sleep / restart), and bodies that are not valid JSON.
  A 404 is cached as a marker so it is not retried forever.
* **Auditable** - every network fetch is appended to ``data/raw/_fetch_log.jsonl``.
"""
from __future__ import annotations

import gzip
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config


class NotFound(Exception):
    """The API answered 404 for this resource (cached, never retried)."""


@dataclass
class Fetched:
    data: Any
    from_cache: bool
    nbytes: int = 0


class RateLimiter:
    """Global minimum interval between request *starts*, across all threads."""

    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.min_interval
        delay = start - now
        if delay > 0:
            time.sleep(delay)


class ApiClient:
    def __init__(
        self,
        base: str = config.API_BASE,
        raw_dir: Path = config.RAW_DIR,
        min_interval: float = config.DEFAULT_MIN_INTERVAL_S,
        timeout: float = config.REQUEST_TIMEOUT_S,
        max_retries: int = config.MAX_RETRIES,
        user_agent: str = config.USER_AGENT,
    ):
        self.base = base.rstrip("/")
        self.raw_dir = Path(raw_dir)
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = user_agent
        self.limiter = RateLimiter(min_interval)
        self._log_lock = threading.Lock()
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.stats = {"network": 0, "cache": 0, "retries": 0, "not_found": 0, "bytes": 0}
        self._stats_lock = threading.Lock()

    # ------------------------------------------------------------------ cache
    def cache_path(self, kind: str, key: str) -> Path:
        return self.raw_dir / kind / f"{key}.json.gz"

    def _marker_path(self, kind: str, key: str) -> Path:
        return self.raw_dir / kind / f"{key}.404"

    def is_cached(self, kind: str, key: str) -> bool:
        return self.cache_path(kind, key).exists() or self._marker_path(kind, key).exists()

    def read_cached(self, kind: str, key: str) -> Any:
        """Load a cached response (raises NotFound if a 404 marker is cached)."""
        if self._marker_path(kind, key).exists():
            raise NotFound(f"{kind}/{key}")
        with gzip.open(self.cache_path(kind, key), "rb") as fh:
            return json.loads(fh.read())

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".tmp{os.getpid()}-{threading.get_ident()}")
        tmp.write_bytes(payload)
        tmp.replace(path)

    def _log(self, record: dict) -> None:
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with self._log_lock:
            with open(self.raw_dir / "_fetch_log.jsonl", "a", encoding="utf-8") as fh:
                fh.write(line)

    def _bump(self, **kw: int) -> None:
        with self._stats_lock:
            for k, v in kw.items():
                self.stats[k] += v

    # ---------------------------------------------------------------- network
    def _http_get(self, url: str) -> bytes:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return body

    def get_json(self, path: str, kind: str, key: str, refresh: bool = False) -> Fetched:
        """Fetch `path` (e.g. '/events/13'), caching under raw/<kind>/<key>.json.gz."""
        if not refresh and self.is_cached(kind, key):
            self._bump(cache=1)
            return Fetched(self.read_cached(kind, key), True)

        url = self.base + path
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                self._bump(retries=1)
                time.sleep(config.BACKOFF_BASE_S * (2 ** (attempt - 1)))
            self.limiter.wait()
            t0 = time.monotonic()
            try:
                body = self._http_get(url)
                data = json.loads(body)  # validate BEFORE caching
            except urllib.error.HTTPError as e:
                last_err = e
                self._log({"ts": time.time(), "url": path, "status": e.code, "attempt": attempt})
                if e.code == 404:
                    self._atomic_write(self._marker_path(kind, key), b"")
                    self._bump(not_found=1)
                    raise NotFound(path) from e
                if e.code in (429, 500, 502, 503, 504):
                    ra = e.headers.get("Retry-After") if e.headers else None
                    if ra and ra.isdigit():
                        time.sleep(min(int(ra), 120))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError, OSError) as e:
                last_err = e
                self._log({"ts": time.time(), "url": path, "status": "error", "err": repr(e)[:200], "attempt": attempt})
                continue

            self._atomic_write(self.cache_path(kind, key), gzip.compress(body, compresslevel=6))
            self._log(
                {
                    "ts": time.time(),
                    "url": path,
                    "status": 200,
                    "bytes": len(body),
                    "secs": round(time.monotonic() - t0, 3),
                    "attempt": attempt,
                }
            )
            self._bump(network=1, bytes=len(body))
            return Fetched(data, False, len(body))

        raise RuntimeError(f"giving up on {url} after {self.max_retries + 1} attempts: {last_err!r}")

    # -------------------------------------------------------- typed endpoints
    def tournaments_index(self, refresh: bool = False) -> Fetched:
        return self.get_json("/tournaments/", "index", "tournaments", refresh)

    def tournament(self, tid: int, refresh: bool = False) -> Fetched:
        return self.get_json(f"/tournaments/{tid}", "tournaments", str(tid), refresh)

    def event(self, eid: int, refresh: bool = False) -> Fetched:
        return self.get_json(f"/events/{eid}", "events", str(eid), refresh)

    def scores(self, eid: int, refresh: bool = False) -> Fetched:
        return self.get_json(f"/events/{eid}/scores", "scores", str(eid), refresh)
