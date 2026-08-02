from __future__ import annotations

import logging
import time
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
                delay = min(2**attempt, 16)
                LOGGER.warning("API request failed; retrying in %s second(s): %s", delay, type(exc).__name__)
                time.sleep(delay)
        raise ApiError(f"API request failed: {method} {url}: {last_error}") from last_error

