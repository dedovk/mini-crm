import base64
import json

import pytest

from crm_sync.config import ConfigurationError, Settings


def configure_required_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    credentials = {
        "type": "service_account",
        "private_key": "not-a-real-key",
        "client_email": "crm@example.test",
    }
    encoded = base64.b64encode(json.dumps(credentials).encode()).decode()
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", encoded)
    monkeypatch.setenv("GOOGLE_SPREADSHEET_ID", "spreadsheet-id")
    monkeypatch.setenv("GOOGLE_WORKSHEET_NAME", "БСК")


def test_settings_reject_malformed_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_B64", "not base64!")

    with pytest.raises(ConfigurationError, match="not valid Base64 JSON"):
        Settings.from_env()


def test_settings_reject_invalid_runtime_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_required_environment(monkeypatch)
    monkeypatch.setenv("HTTP_TIMEOUT_SECONDS", "0")

    with pytest.raises(ConfigurationError, match="HTTP_TIMEOUT_SECONDS must be at least 1"):
        Settings.from_env()


def test_settings_reject_unknown_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_required_environment(monkeypatch)
    monkeypatch.setenv("APP_TIMEZONE", "Mars/Olympus")

    with pytest.raises(ConfigurationError, match="APP_TIMEZONE is unknown"):
        Settings.from_env()
