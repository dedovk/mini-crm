from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from crm_sync.models import (
    Order,
    OrderItem,
    ResolvedSupplierCost,
    ShipmentStatus,
    ShipmentUpdateResult,
    SupplierCostBatch,
    SupplierCostRecord,
    SupplierCostUpdateResult,
    SyncHealthState,
)
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


class ProductionSheetsStub(SheetsStub):
    def __init__(self) -> None:
        self.schema_modes: list[bool] = []
        self.health_calls: list[list[str]] = []
        self.backups = 0
        self.force_rebuild = False
        self.refused_orders = False
        self.supplier_cost_calls: list[dict[str, ResolvedSupplierCost]] = []

    def ensure_schema(self, *, apply_changes: bool) -> None:
        self.schema_modes.append(apply_changes)

    def record_completion_observations(self, orders, *, observed_at):
        return ()

    def backfill_completion_state(self, *, observed_at) -> int:
        return 0

    def refresh_order_details(self, orders) -> int:
        return 0

    def update_shipment_statuses(self, statuses):
        return ShipmentUpdateResult()

    def latest_layout_day(self):
        return datetime.now(UTC).date()

    def has_refused_orders(self) -> bool:
        return self.refused_orders

    def create_backup(self, *, created_at) -> str:
        self.backups += 1
        return "backup"

    def append_orders(self, orders, statuses, **kwargs) -> int:
        assert orders == []
        self.force_rebuild = kwargs.get("force_rebuild", False)
        return 0

    def update_order_expenses(self, expenses, *, source: str) -> int:
        return 0

    def update_supplier_costs(self, costs, *, observed_at) -> SupplierCostUpdateResult:
        self.supplier_cost_calls.append(dict(costs))
        return SupplierCostUpdateResult(cell_updates=len(costs))

    def append_audit_events(self, events) -> int:
        return 0

    def record_sync_health(self, failed_components, *, occurred_at):
        self.health_calls.append(failed_components)
        return SyncHealthState()


class RepairableFormulaErrorSheetsStub(ProductionSheetsStub):
    def __init__(self) -> None:
        super().__init__()
        self.validate_calls = 0
        self.refresh_calls = 0

    def validate_integrity(self):
        from crm_sync.integrity import IntegrityReport

        self.validate_calls += 1
        if self.validate_calls == 1:
            return IntegrityReport(errors=("formula error at R5: #VALUE!",))
        return IntegrityReport()

    def refresh_order_details(self, orders) -> int:
        self.refresh_calls += 1
        return 2


class NovaPoshtaStub:
    def get_statuses(self, tracking_numbers: list[str]) -> dict[str, str]:
        return {}


class FailingNovaPoshtaStub:
    def get_statuses(self, tracking_numbers: list[str]):
        raise RuntimeError("tracking timeout")


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


class CancelledPromSource:
    source = "prom"

    def fetch_orders(self, since: datetime):
        return [
            Order(
                source="prom",
                external_id="417709650",
                created_at=datetime(2026, 7, 25, tzinfo=UTC),
                completed_at=datetime(2026, 8, 11, tzinfo=UTC),
                customer_name="",
                city="",
                phone="",
                tracking_number="",
                total=Decimal(0),
                payment_method="",
                note="",
                sender="",
                source_status="Скасовано",
            )
        ]


class FailingExpenseSource:
    source = "rozetka"

    def fetch_expenses(self, since: datetime):
        raise RuntimeError("finance access denied")


class SupplierCostSourceStub:
    source = "supplier-imaxi"

    def fetch_costs(self):
        return SupplierCostBatch(
            source=self.source,
            values={"20451510462545": SupplierCostRecord.cost(Decimal("1079"))},
            warnings=("supplier duplicate was ignored",),
        )


class FailingSupplierCostSource:
    source = "supplier-imaxi"

    def fetch_costs(self):
        raise RuntimeError("supplier sheet timeout")


class SupplierCostWriteFailureSheets(ProductionSheetsStub):
    def update_supplier_costs(self, costs, *, observed_at) -> SupplierCostUpdateResult:
        raise RuntimeError("write quota exceeded")


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


def test_nova_poshta_failure_preserves_order_sync_and_updates_health() -> None:
    sheets = ProductionSheetsStub()
    service = SyncService(
        sheets=sheets,  # type: ignore[arg-type]
        nova_poshta=FailingNovaPoshtaStub(),  # type: ignore[arg-type]
        sources=[SuccessfulSource()],  # type: ignore[list-item]
        timezone="Europe/Kyiv",
        lookback_days=7,
        sender_default="наш",
        dry_run=False,
    )

    result = service.run()

    assert sheets.health_calls == [["nova-poshta"]]
    assert result.shipment_statuses == 0
    assert result.warnings == (
        "nova-poshta tracking is unavailable; existing shipment statuses were preserved: "
        "tracking timeout",
    )


def test_supplier_costs_are_imported_and_warnings_are_reported() -> None:
    sheets = ProductionSheetsStub()
    service = SyncService(
        sheets=sheets,  # type: ignore[arg-type]
        nova_poshta=NovaPoshtaStub(),  # type: ignore[arg-type]
        sources=[SuccessfulSource()],  # type: ignore[list-item]
        supplier_cost_sources=[SupplierCostSourceStub()],
        timezone="Europe/Kyiv",
        lookback_days=7,
        sender_default="наш",
        dry_run=False,
    )

    result = service.run()

    assert sheets.supplier_cost_calls == [
        {
            "20451510462545": ResolvedSupplierCost(
                "supplier-imaxi", SupplierCostRecord.cost(Decimal("1079"))
            )
        }
    ]
    assert result.supplier_cost_updates == 1
    assert "supplier duplicate was ignored" in result.warnings


def test_supplier_network_failure_preserves_existing_costs_and_marks_health() -> None:
    sheets = ProductionSheetsStub()
    service = SyncService(
        sheets=sheets,  # type: ignore[arg-type]
        nova_poshta=NovaPoshtaStub(),  # type: ignore[arg-type]
        sources=[SuccessfulSource()],  # type: ignore[list-item]
        supplier_cost_sources=[FailingSupplierCostSource()],
        timezone="Europe/Kyiv",
        lookback_days=7,
        sender_default="наш",
        dry_run=False,
    )

    result = service.run()

    assert sheets.supplier_cost_calls == []
    assert sheets.health_calls == [["supplier-imaxi"]]
    assert result.supplier_cost_updates == 0
    assert result.warnings == (
        "supplier-imaxi costs are unavailable; existing sheet values were preserved: "
        "supplier sheet timeout",
    )


def test_supplier_cost_write_failure_does_not_abort_completed_order_sync() -> None:
    sheets = SupplierCostWriteFailureSheets()
    service = SyncService(
        sheets=sheets,  # type: ignore[arg-type]
        nova_poshta=NovaPoshtaStub(),  # type: ignore[arg-type]
        sources=[SuccessfulSource()],  # type: ignore[list-item]
        supplier_cost_sources=[SupplierCostSourceStub()],
        timezone="Europe/Kyiv",
        lookback_days=7,
        sender_default="наш",
        dry_run=False,
    )

    result = service.run()

    assert result.supplier_cost_updates == 0
    assert sheets.health_calls == [["supplier-cost-write"]]
    assert any("write quota exceeded" in warning for warning in result.warnings)


def test_cross_supplier_conflict_is_quarantined_independently_of_order() -> None:
    cost = SupplierCostRecord.cost(Decimal("100"))
    other_cost = SupplierCostRecord.cost(Decimal("120"))
    resolved = SyncService._resolve_supplier_costs(
        (
            SupplierCostBatch(
                source="supplier-a",
                values={"20451510462545": cost, "20451509877182": cost},
            ),
            SupplierCostBatch(
                source="supplier-b",
                values={"20 4515 1046 2545": other_cost, "20451509877182": cost},
            ),
        )
    )

    assert resolved.values == {
        "20451509877182": ResolvedSupplierCost("supplier-b", cost)
    }
    assert resolved.warnings == (
        "Supplier TTN 20451510462545 conflicts between supplier-a and supplier-b; "
        "it was not imported",
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


def test_recently_shipped_old_order_is_selected_by_shipping_date() -> None:
    order = Order(
        source="rozetka",
        external_id="shipped",
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
        completed_at=datetime(2026, 8, 10, tzinfo=UTC),
        customer_name="Customer",
        city="Kyiv",
        phone="+380501234567",
        tracking_number="RMP-123456789",
        total=Decimal(100),
        payment_method="",
        note="",
        sender="",
        completion_is_exact=False,
        source_status="Відправлено",
        items=[OrderItem("Product", "SKU", Decimal(1), Decimal(100), Decimal(100))],
    )

    selection = SyncService._select_new_orders(
        [order], set(), cutoff=date(2026, 8, 9)
    )

    assert selection.orders == (order,)
    assert selection.stale_count == 0


def test_recent_unseen_completed_rozetka_order_is_inserted() -> None:
    order = Order(
        source="rozetka",
        external_id="completed",
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
        completed_at=datetime(2026, 8, 11, tzinfo=UTC),
        customer_name="Customer",
        city="Kyiv",
        phone="+380501234567",
        tracking_number="RMP-123456789",
        total=Decimal(100),
        payment_method="",
        note="",
        sender="",
        source_status="Виконано",
        items=[OrderItem("Product", "SKU", Decimal(1), Decimal(100), Decimal(100))],
    )

    selection = SyncService._select_new_orders([order], set(), cutoff=date(2026, 8, 9))

    assert selection.orders == (order,)
    assert selection.stale_count == 0


def test_stale_unseen_completed_rozetka_order_is_not_inserted() -> None:
    order = Order(
        source="rozetka",
        external_id="stale-completed",
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
        completed_at=datetime(2026, 7, 2, tzinfo=UTC),
        customer_name="Customer",
        city="Kyiv",
        phone="+380501234567",
        tracking_number="RMP-123456789",
        total=Decimal(100),
        payment_method="",
        note="",
        sender="",
        source_status="Виконано",
        items=[OrderItem("Product", "SKU", Decimal(1), Decimal(100), Decimal(100))],
    )

    selection = SyncService._select_new_orders([order], set(), cutoff=date(2026, 8, 9))

    assert selection.orders == ()
    assert selection.stale_count == 1


def test_prepayment_is_inferred_from_nova_poshta_cod_amount() -> None:
    order = Order(
        source="prom",
        external_id="421221060",
        created_at=datetime(2026, 8, 13, tzinfo=UTC),
        completed_at=datetime(2026, 8, 13, tzinfo=UTC),
        customer_name="Customer",
        city="Kyiv",
        phone="+380501234567",
        tracking_number="20451510462545",
        total=Decimal(4449),
        payment_method="наложка",
        note="",
        sender="наш",
        items=[OrderItem("Product", "KR65-GR-5P", Decimal(1), Decimal(4449), Decimal(4449))],
    )
    statuses = {
        "20451510462545": ShipmentStatus(
            tracking_number="20451510462545",
            status="Прямує до покупця",
            status_code="5",
            redelivery_sum=Decimal(3449),
        )
    }

    inferred = SyncService._apply_tracking_prepayments([order], statuses)

    assert inferred == 1
    assert order.prepayment == Decimal("1000.00")
    assert order.payment_method == "смешанная"


def test_recently_modified_opencart_order_is_selected_despite_old_completion() -> None:
    order = Order(
        source="opencart",
        external_id="934",
        created_at=datetime(2026, 7, 30, tzinfo=UTC),
        completed_at=datetime(2026, 7, 30, tzinfo=UTC),
        updated_at=datetime(2026, 8, 11, tzinfo=UTC),
        customer_name="Customer",
        city="Kyiv",
        phone="+380501234567",
        tracking_number="20451234567890",
        total=Decimal(100),
        payment_method="",
        note="",
        sender="",
        source_status="Виконано",
        items=[OrderItem("Product", "SKU", Decimal(1), Decimal(100), Decimal(100))],
    )

    selection = SyncService._select_new_orders([order], set(), cutoff=date(2026, 8, 4))

    assert selection.orders == (order,)
    assert selection.stale_count == 0


def test_production_run_performs_preflight_postflight_and_health_update() -> None:
    sheets = ProductionSheetsStub()
    service = SyncService(
        sheets=sheets,  # type: ignore[arg-type]
        nova_poshta=NovaPoshtaStub(),  # type: ignore[arg-type]
        sources=[SuccessfulSource()],  # type: ignore[list-item]
        timezone="Europe/Kyiv",
        lookback_days=7,
        sender_default="наш",
        dry_run=False,
    )

    result = service.run()

    assert sheets.schema_modes == [False, True]
    assert sheets.health_calls == [[]]
    assert result.health.consecutive_failures == 0


def test_production_run_repairs_formula_errors_before_fatal_postflight() -> None:
    sheets = RepairableFormulaErrorSheetsStub()
    service = SyncService(
        sheets=sheets,  # type: ignore[arg-type]
        nova_poshta=NovaPoshtaStub(),  # type: ignore[arg-type]
        sources=[SuccessfulSource()],  # type: ignore[list-item]
        timezone="Europe/Kyiv",
        lookback_days=7,
        sender_default="наш",
        dry_run=False,
    )

    result = service.run()

    assert sheets.validate_calls == 2
    assert sheets.refresh_calls == 1
    assert result.refreshed_cells == 2
    assert result.warnings == (
        "Sheet contains formula errors before repair; production sync will try to refresh formulas and values.",
    )


def test_production_run_advances_daily_layout_without_new_orders() -> None:
    sheets = ProductionSheetsStub()
    sheets.latest_layout_day = lambda: date(2020, 1, 1)
    service = SyncService(
        sheets=sheets,  # type: ignore[arg-type]
        nova_poshta=NovaPoshtaStub(),  # type: ignore[arg-type]
        sources=[SuccessfulSource()],  # type: ignore[list-item]
        timezone="Europe/Kyiv",
        lookback_days=7,
        sender_default="наш",
        dry_run=False,
    )

    result = service.run()

    assert result.layout_advanced
    assert sheets.force_rebuild
    assert sheets.backups == 1


def test_production_run_rebuilds_and_backs_up_when_refused_order_exists() -> None:
    sheets = ProductionSheetsStub()
    sheets.refused_orders = True
    service = SyncService(
        sheets=sheets,  # type: ignore[arg-type]
        nova_poshta=NovaPoshtaStub(),  # type: ignore[arg-type]
        sources=[SuccessfulSource()],  # type: ignore[list-item]
        timezone="Europe/Kyiv",
        lookback_days=7,
        sender_default="наш",
        dry_run=False,
    )

    service.run()

    assert sheets.force_rebuild
    assert sheets.backups == 1


def test_production_run_removes_existing_cancelled_prom_order() -> None:
    sheets = ProductionSheetsStub()
    sheets.read_existing_sync_keys = lambda: {"prom:417709650"}
    captured: dict = {}

    def append_orders(orders, statuses, **kwargs):
        captured.update(kwargs)
        return 0

    sheets.append_orders = append_orders
    now = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    sheets.latest_layout_day = lambda: now.date()
    service = SyncService(
        sheets=sheets,  # type: ignore[arg-type]
        nova_poshta=NovaPoshtaStub(),  # type: ignore[arg-type]
        sources=[CancelledPromSource()],  # type: ignore[list-item]
        timezone="Europe/Kyiv",
        lookback_days=30,
        sender_default="наш",
        dry_run=False,
        clock=lambda: now,
    )

    service.run()

    assert captured["force_rebuild"] is True
    assert captured["excluded_sync_keys"] == {"prom:417709650"}
    assert sheets.backups == 1
