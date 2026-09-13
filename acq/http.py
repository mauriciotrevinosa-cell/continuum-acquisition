"""Polite HTTP client: disk cache, per-host rate limit, retries.

Honest identification only: no user-agent spoofing, no header games, no
anti-bot evasion. A provider that refuses or disables its API is reported as
unavailable and skipped for the run.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .util import LOG, read_json, write_json

USER_AGENT = "ContinuumAcquisition/1.0 (personal library cataloguing; non-commercial)"
DEFAULT_INTERVALS = {"api.mangaupdates.com": 1.0, "ndlsearch.ndl.go.jp": 1.0, "graphql.anilist.co": 2.1}


class ProviderUnavailable(RuntimeError):
    """The provider cannot be used right now (disabled, refused, offline)."""


class HttpClient:
    def __init__(self, cache_dir: str, *, user_agent: str = USER_AGENT, intervals: dict | None = None,
                 ttl_days: float = 30, offline: bool = False, timeout: float = 60):
        self.cache_dir = cache_dir
        self.user_agent = user_agent
        self.intervals = dict(DEFAULT_INTERVALS, **(intervals or {}))
        self.ttl_days = ttl_days
        self.offline = offline
        self.timeout = timeout
        self._last: dict[str, float] = {}
        self.stats = {"cache_hits": 0, "requests": 0, "errors": 0}

    def _key_path(self, method: str, url: str, body) -> str:
        key = hashlib.sha1(f"{method} {url} {json.dumps(body, sort_keys=True) if body is not None else ''}"
                           .encode("utf-8")).hexdigest()
        return os.path.join(self.cache_dir, key[:2], key + ".json")

    def _throttle(self, host: str) -> None:
        interval = self.intervals.get(host, 1.0)
        wait = self._last.get(host, 0) + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()

    def request(self, method: str, url: str, *, body=None, headers: dict | None = None,
                ttl_days: float | None = None, use_cache: bool = True) -> tuple[int, bytes]:
        path = self._key_path(method, url, body)
        ttl = self.ttl_days if ttl_days is None else ttl_days
        if use_cache and ttl > 0:
            rec = read_json(path)
            if rec and (time.time() - rec.get("fetched_epoch", 0)) < ttl * 86400:
                self.stats["cache_hits"] += 1
                return rec["status"], base64.b64decode(rec["body_b64"])
        if self.offline:
            raise ProviderUnavailable(f"offline and not cached: {url}")
        data = None
        hdrs = {"User-Agent": self.user_agent, "Accept": "application/json, application/xml;q=0.9, */*;q=0.5"}
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif isinstance(body, bytes):
            data = body
        hdrs.update(headers or {})
        host = urlparse(url).hostname or ""
        status, payload = 0, b""
        for attempt in range(4):
            self._throttle(host)
            self.stats["requests"] += 1
            try:
                req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    status, payload = resp.status, resp.read()
                break
            except urllib.error.HTTPError as err:
                status, payload = err.code, err.read()
                if status in (429, 500, 502, 503, 504) and attempt < 3:
                    retry_after = err.headers.get("Retry-After")
                    delay = min(float(retry_after), 60.0) if (retry_after or "").isdigit() else 2 ** (attempt + 1)
                    LOG.debug("HTTP %s from %s, retrying in %.0fs", status, host, delay)
                    time.sleep(delay)
                    continue
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
                self.stats["errors"] += 1
                if attempt < 3:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise ProviderUnavailable(f"{host}: {err}") from err
        if use_cache and status == 200:
            write_json(path, {"url": url, "method": method, "status": status, "fetched_epoch": time.time(),
                              "body_b64": base64.b64encode(payload).decode("ascii")})
        return status, payload

    def json(self, method: str, url: str, **kw) -> tuple[int, object]:
        status, payload = self.request(method, url, **kw)
        try:
            return status, (json.loads(payload.decode("utf-8")) if payload else None)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return status, None

    def download(self, url: str, dest_partial: str, *, headers: dict | None = None) -> int:
        """Stream a file to a partial path (caller verifies and renames)."""
        host = urlparse(url).hostname or ""
        self._throttle(host)
        hdrs = {"User-Agent": self.user_agent, **(headers or {})}
        req = urllib.request.Request(url, headers=hdrs)
        total = 0
        with urllib.request.urlopen(req, timeout=self.timeout) as resp, open(dest_partial, "wb") as fh:
            while True:
                block = resp.read(1 << 20)
                if not block:
                    break
                fh.write(block)
                total += len(block)
        return total
