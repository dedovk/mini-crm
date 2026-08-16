from unittest.mock import Mock, patch

import pytest
import requests

from crm_sync.clients.http import ApiError, HttpClient


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


def test_http_client_exposes_non_retryable_status_code() -> None:
    unauthorized = response(401)
    unauthorized.raise_for_status.side_effect = requests.HTTPError(
        "unauthorized", response=unauthorized
    )
    client = HttpClient(max_retries=0)
    client.session.request = Mock(return_value=unauthorized)

    with pytest.raises(ApiError) as error:
        client.request_json("GET", "https://example.test/orders")

    assert error.value.status_code == 401


def test_http_client_retries_invalid_json_once() -> None:
    malformed = response(200)
    malformed.json.side_effect = requests.JSONDecodeError("invalid JSON", "<html>", 0)
    successful = response(200)
    client = HttpClient(max_retries=1)
    client.session.request = Mock(side_effect=[malformed, successful])

    with patch("crm_sync.clients.http.time.sleep") as sleep:
        payload = client.request_json("GET", "https://example.test/orders")

    assert payload == {"ok": True}
    sleep.assert_called_once_with(1)
