from unittest.mock import Mock, patch

import requests

from crm_sync.clients.http import HttpClient


def response(status: int, *, retry_after: str = "") -> Mock:
    value = Mock(spec=requests.Response)
    value.status_code = status
    value.headers = {"Retry-After": retry_after} if retry_after else {}
    value.json.return_value = {"ok": True}
    value.raise_for_status.return_value = None
    return value


def test_http_client_honors_retry_after_for_rate_limit() -> None:
    limited = response(429, retry_after="45")
    successful = response(200)
    client = HttpClient(max_retries=1)
    client.session.request = Mock(side_effect=[limited, successful])

    with patch("crm_sync.clients.http.time.sleep") as sleep:
        payload = client.request_json("GET", "https://example.test/orders")

    assert payload == {"ok": True}
    sleep.assert_called_once_with(45)


def test_http_client_uses_longer_fallback_for_rate_limit() -> None:
    limited = response(429)
    successful = response(200)
    client = HttpClient(max_retries=1)
    client.session.request = Mock(side_effect=[limited, successful])

    with patch("crm_sync.clients.http.time.sleep") as sleep:
        client.request_json("GET", "https://example.test/orders")

    sleep.assert_called_once_with(30)
