from datetime import UTC, datetime
from decimal import Decimal

import pytest

from crm_sync.models import Order, OrderItem
from crm_sync.services import SourceSyncError, SyncService


class SheetsStub:
    def ensure_schema(self, *, apply_changes: bool) -> None:
        assert not apply_changes

    def read_existing_sync_keys(self) -> set[str]:
        return set()

    def validate_integrity(self):
        from crm_sync.integrity import IntegrityReport

        return IntegrityReport()

    def pending_tracking_numbers(self) -> list[str]:
        return []


class NovaPoshtaStub:
    def get_statuses(self, tracking_numbers: list[str]) -> dict[str, str]:
        assert tracking_numbers == []
        return {}


class FailingSource:
    source = "prom"

    def fetch_orders(self, since: datetime):
        raise RuntimeError("rate limited")


class SuccessfulSource:
    source = "rozetka"

    def __init__(self) -> None:
        self.called = False

    def fetch_orders(self, since: datetime):
        self.called = True
        return []


class FailingExpenseSource:
    source = "rozetka"

    def fetch_expenses(self, since: datetime):
        raise RuntimeError("finance access denied")


class StaleInexactSource:
    source = "prom"

    def fetch_orders(self, since: datetime):
        return [
            Order(
                source="prom",
                external_id="old-order",
                created_at=datetime(2020, 1, 1, tzinfo=UTC),
                completed_at=datetime.now(UTC),
                customer_name="Customer",
                city="Kyiv",
                phone="+380501234567",
                tracking_number="20451234567890",
                total=Decimal(100),
                payment_method="",
                note="",
                sender="",
                completion_is_exact=False,
                items=[
                    OrderItem(
                        name="Product",
                        product_code="SKU",
                        quantity=Decimal(1),
                        unit_price=Decimal(100),
                        line_total=Decimal(100),
                    )
                ],
            )
        ]


def test_source_failure_is_reported_after_other_sources_continue() -> None:
    successful = SuccessfulSource()
    service = SyncService(
        sheets=SheetsStub(),  # type: ignore[arg-type]
        nova_poshta=NovaPoshtaStub(),  # type: ignore[arg-type]
        sources=[FailingSource(), successful],  # type: ignore[list-item]
        timezone="Europe/Kyiv",
        lookback_days=7,
        sender_default="-",
        dry_run=True,
    )

    with pytest.raises(SourceSyncError, match="prom") as error:
        service.run()

    assert successful.called
    assert error.value.result.failed_sources == ("prom",)
    assert error.value.result.source_orders == {"rozetka": 0}


def test_optional_finance_failure_does_not_fail_order_sync() -> None:
    successful = SuccessfulSource()
    service = SyncService(
        sheets=SheetsStub(),  # type: ignore[arg-type]
        nova_poshta=NovaPoshtaStub(),  # type: ignore[arg-type]
        sources=[successful],  # type: ignore[list-item]
        expense_source=FailingExpenseSource(),  # type: ignore[arg-type]
        timezone="Europe/Kyiv",
        lookback_days=7,
        sender_default="-",
        dry_run=True,
    )

    result = service.run()

    assert successful.called
    assert result.failed_sources == ()
    assert result.source_orders == {"rozetka": 0}
    assert result.warnings == (
        "rozetka finance is unavailable; existing sheet values were preserved: finance access denied",
    )


def test_deep_run_does_not_backfill_previously_unseen_stale_order() -> None:
    service = SyncService(
        sheets=SheetsStub(),  # type: ignore[arg-type]
        nova_poshta=NovaPoshtaStub(),  # type: ignore[arg-type]
        sources=[StaleInexactSource()],  # type: ignore[list-item]
        timezone="Europe/Kyiv",
        lookback_days=30,
        new_order_max_age_days=7,
        sender_default="-",
        dry_run=True,
    )

    result = service.run()

    assert result.stale_orders == 1
    assert result.new_orders == 0
