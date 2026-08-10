"""Thin HTTP client for ESPN's public JSON APIs.

Every response is archived to data/raw as JSON. Parsing bugs are then fixable
without re-hitting the network, and the raw payloads stay auditable.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import requests

from . import config

log = logging.getLogger(__name__)


class ESPNError(RuntimeError):
    pass


class ESPNNotFound(ESPNError):
    """A 4xx: the resource is absent, not temporarily unavailable.

    Kept apart from ESPNError so it can skip the retry loop. Some endpoints
    404 as a normal answer - an athlete with no stat line for an event - and
    retrying that four times with backoff costs 14 seconds to learn nothing.
    """


class ESPNClient:
    def __init__(
        self,
        cache_dir: Path = config.RAW_DIR,
        use_cache: bool = True,
        delay: float = config.REQUEST_DELAY,
        max_retries: int = config.MAX_RETRIES,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.use_cache = use_cache
        self.delay = delay
        self.max_retries = max_retries
        self._last_request_at = 0.0
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        # Only override the default when config asks for it. Assigning None into
        # a Session's headers does not clear the header, it sends the literal
        # string "None" - which is exactly the kind of unrecognised UA that
        # earns a 403.
        if config.USER_AGENT is not None:
            self.session.headers["User-Agent"] = config.USER_AGENT

    # ---------------------------------------------------------------- caching

    def _cache_path(self, cache_key: str) -> Path:
        return self.cache_dir / f"{cache_key}.json"

    # read_cache/write_cache are public so a caller that assembles one document
    # out of several responses - a paginated endpoint, say - can archive the
    # merged result under a single key instead of one file per page.
    def read_cache(self, cache_key: str) -> dict | None:
        path = self._cache_path(cache_key)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("Discarding unreadable cache file %s", path)
            return None

    def write_cache(self, cache_key: str, payload: dict) -> None:
        path = self._cache_path(cache_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    # ------------------------------------------------------------------ fetch

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_request_at = time.monotonic()

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        cache_key: str | None = None,
        force: bool = False,
    ) -> dict:
        """GET a JSON document, honouring the on-disk archive when possible."""
        if cache_key and self.use_cache and not force:
            cached = self.read_cache(cache_key)
            if cached is not None:
                log.debug("cache hit %s", cache_key)
                return cached

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                response = self.session.get(url, params=params, timeout=45)
                if response.status_code == 429 or response.status_code >= 500:
                    raise ESPNError(f"HTTP {response.status_code} from {response.url}")
                if 400 <= response.status_code < 500:
                    raise ESPNNotFound(f"HTTP {response.status_code} from {response.url}")
                response.raise_for_status()
                payload = response.json()
            except ESPNNotFound:
                raise
            except (requests.RequestException, ESPNError, json.JSONDecodeError) as exc:
                last_error = exc
                backoff = min(2 ** attempt, 30)
                log.warning("request failed (attempt %d/%d): %s", attempt, self.max_retries, exc)
                if attempt < self.max_retries:
                    time.sleep(backoff)
                continue

            if cache_key:
                self.write_cache(cache_key, payload)
            return payload

        raise ESPNError(f"Giving up on {url} after {self.max_retries} attempts") from last_error
