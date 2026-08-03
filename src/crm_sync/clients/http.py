from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)


class ApiError(RuntimeError):
    """External API returned an unusable response."""


class HttpClient:
    def __init__(self, *, timeout: int = 30, max_retries: int = 4) -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "marketplace-crm-sync/0.1"})

    def request_json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any] | list[Any]:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.HTTPError(f"retryable HTTP {response.status_code}", response=response)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, (dict, list)):
                    raise ApiError(f"Unexpected JSON type from {url}")
                return payload
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as exc:
                last_error = exc
                retryable = not isinstance(exc, requests.HTTPError) or (
                    exc.response is not None
                    and (exc.response.status_code == 429 or exc.response.status_code >= 500)
                )
                if not retryable or attempt >= self.max_retries:
                    break
                delay = self._retry_delay(exc, attempt)
                status = exc.response.status_code if isinstance(exc, requests.HTTPError) and exc.response else None
                LOGGER.warning(
                    "API request failed%s; retrying in %s second(s): %s",
                    f" with HTTP {status}" if status else "",
                    delay,
                    type(exc).__name__,
                )
                time.sleep(delay)
        raise ApiError(f"API request failed: {method} {url}: {last_error}") from last_error

    @staticmethod
    def _retry_delay(exc: Exception, attempt: int) -> int:
        if not isinstance(exc, requests.HTTPError) or exc.response is None:
            return min(2**attempt, 16)
        if exc.response.status_code != 429:
            return min(2**attempt, 16)

        retry_after = exc.response.headers.get("Retry-After", "").strip()
        if retry_after.isdigit():
            return min(max(int(retry_after), 1), 120)
        if retry_after:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                seconds = int((retry_at - datetime.now(UTC)).total_seconds()) + 1
                return min(max(seconds, 1), 120)
            except (TypeError, ValueError, OverflowError):
                pass

        # Prom does not always include Retry-After. Short exponential delays are
        # insufficient for its rate-limit window, so start with a conservative pause.
        return min(30 * (2**attempt), 120)
