from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from crm_sync import installment_backfill
from crm_sync.config import ConfigurationError
from crm_sync.installment_backfill import _backfill_days
from crm_sync.models import InstallmentReconciliationResult, Order, OrderItem


@pytest.mark.parametrize("value", ["1", "120", "730"])
def test_installment_backfill_days_accepts_bounded_integer(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("INSTALLMENT_BACKFILL_DAYS", value)

    assert _backfill_days() == int(value)


@pytest.mark.parametrize("value", ["", "0", "731", "invalid", "1.5"])
def test_installment_backfill_days_rejects_unsafe_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("INSTALLMENT_BACKFILL_DAYS", value)

    with pytest.raises(ConfigurationError):
        _backfill_days()


def test_installment_backfill_reconciles_once_and_tracks_missing_sheet_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INSTALLMENT_BACKFILL_DAYS", "90")
    settings = SimpleNamespace(
        log_level="INFO",
        prom_token="token",
        timezone="Europe/Kyiv",
        http_timeout=30,
        http_max_retries=4,
        prom_base_url="https://example.test",
        google_service_account_info={},
        google_spreadsheet_id="sheet",
        google_worksheet_name="CRM",
        google_header_row=4,
        sender_options=(),
        dry_run=False,
    )
    diagnostics = SimpleNamespace(degraded=False)
    fake_prom = SimpleNamespace(installment_detail_diagnostics=diagnostics)
    calls: list[dict[str, object]] = []

    class FakeSheets:
        def installment_reconciliation_order_ids(self) -> set[str]:
            return {"found", "missing"}

        def reconcile_prom_installments(self, orders, **kwargs):
            calls.append({"orders": orders, **kwargs})
            return InstallmentReconciliationResult(
                unresolved_orders=("missing",),
            )

    found = Order(
        source="prom",
        external_id="found",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        completed_at=datetime(2026, 9, 1, tzinfo=UTC),
        customer_name="Customer",
        city="Kyiv",
        phone="+380501234567",
        tracking_number="20451537282409",
        total=Decimal(100),
        payment_method="оплата частями",
        note="",
        sender="наш",
        items=[OrderItem("Товар", "SKU", Decimal(1), Decimal(100), Decimal(100))],
    )
    unrelated = replace(found, external_id="already-reported")
    monkeypatch.setattr(installment_backfill.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(installment_backfill, "HttpClient", lambda **kwargs: object())
    monkeypatch.setattr(
        installment_backfill,
        "PromClient",
        lambda *args, **kwargs: fake_prom,
    )
    monkeypatch.setattr(
        installment_backfill,
        "GoogleSheetsGateway",
        lambda **kwargs: FakeSheets(),
    )
    monkeypatch.setattr(
        installment_backfill,
        "fetch_historical_orders",
        lambda *args, **kwargs: [found, unrelated],
    )

    assert installment_backfill.main() == 0
    assert len(calls) == 1
    assert [order.external_id for order in calls[0]["orders"]] == ["found"]
    assert calls[0]["apply_changes"] is True
    assert calls[0]["expected_order_ids"] == {"found", "missing"}
