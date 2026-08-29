import base64
import json
from decimal import Decimal

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


@pytest.mark.parametrize("value", ["treu", "enabled", "2"])
def test_settings_reject_ambiguous_dry_run_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    configure_required_environment(monkeypatch)
    monkeypatch.setenv("DRY_RUN", value)

    with pytest.raises(ConfigurationError, match="DRY_RUN must be one of"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("ON", True), ("0", False), ("No", False)],
)
def test_settings_parses_explicit_dry_run_values(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    configure_required_environment(monkeypatch)
    monkeypatch.setenv("DRY_RUN", value)

    assert Settings.from_env().dry_run is expected


def test_supplier_sheet_id_is_optional_and_read_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_required_environment(monkeypatch)

    assert Settings.from_env().supplier_imaxi_spreadsheet_id == ""

    monkeypatch.setenv("SUPPLIER_IMAXI_SPREADSHEET_ID", "supplier-sheet")
    assert Settings.from_env().supplier_imaxi_spreadsheet_id == "supplier-sheet"
    assert Settings.from_env().supplier_melad_spreadsheet_id == ""

    monkeypatch.setenv("SUPPLIER_MELAD_SPREADSHEET_ID", "melad-sheet")
    assert Settings.from_env().supplier_melad_spreadsheet_id == "melad-sheet"


def test_prom_installment_fallback_rate_is_explicit_and_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_required_environment(monkeypatch)
    assert Settings.from_env().prom_installment_fallback_rate == 0

    monkeypatch.setenv("PROM_INSTALLMENT_FALLBACK_RATE", "0.037")
    assert Settings.from_env().prom_installment_fallback_rate == Decimal("0.037")

    monkeypatch.setenv("PROM_INSTALLMENT_FALLBACK_RATE", "unknown")
    with pytest.raises(ConfigurationError, match="must be a decimal number"):
        Settings.from_env()

    monkeypatch.setenv("PROM_INSTALLMENT_FALLBACK_RATE", "3.7")
    with pytest.raises(ConfigurationError, match="at most 1"):
        Settings.from_env()
