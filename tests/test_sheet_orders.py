from datetime import UTC, date, datetime
from decimal import Decimal

from crm_sync.models import Order, OrderItem, ShipmentStatus
from crm_sync.sheet_layout import ROW_ORDER, sheet_serial
from crm_sync.sheet_orders import collect_order_groups
from crm_sync.sheet_schema import COLUMNS, LAST_COLUMN


def test_collect_order_groups_normalizes_legacy_rows_without_losing_manual_values() -> None:
    row = [""] * LAST_COLUMN
    row[COLUMNS.source - 1] = "prom"
    row[COLUMNS.tracking_number - 1] = "2045 1234 5678 90"
    row[COLUMNS.order_date - 1] = "05.08.2026 12:30"
    row[COLUMNS.customer - 1] = "Київ, Петренко Іван Іванович"
    row[COLUMNS.sender - 1] = "-"
    row[COLUMNS.cost - 1] = 700
    row[COLUMNS.sync_key - 1] = "prom:1"
    row[COLUMNS.row_type - 1] = ROW_ORDER
    row[COLUMNS.operational_date - 1] = sheet_serial(date(2026, 8, 5))

    groups = collect_order_groups(
        [row], [], {}, sender_default="наш", observation_day=date(2026, 8, 8)
    )

    normalized = groups.rows["prom:1"][0]
    assert normalized[COLUMNS.order_date - 1] == sheet_serial(date(2026, 8, 5))
    assert normalized[COLUMNS.customer - 1] == "Київ, Петренко Іван"
    assert normalized[COLUMNS.sender - 1] == "наш"
    assert normalized[COLUMNS.cost - 1] == 700


def test_collect_order_groups_writes_advertising_once_for_multi_item_order() -> None:
    order = Order(
        source="prom",
        external_id="2",
        created_at=datetime(2026, 8, 7, tzinfo=UTC),
        completed_at=datetime(2026, 8, 8, tzinfo=UTC),
        customer_name="Петренко Іван",
        city="Київ",
        phone="+380501234567",
        tracking_number="20451234567890",
        total=Decimal(300),
        payment_method="наложка",
        note="",
        sender="",
        completion_is_exact=False,
        advertising_cost=Decimal(10),
        items=[
            OrderItem("A", "A-1", Decimal(1), Decimal(100), Decimal(100)),
            OrderItem("B", "B-1", Decimal(1), Decimal(200), Decimal(200)),
        ],
    )

    groups = collect_order_groups(
        [], [order], {}, sender_default="наш", observation_day=date(2026, 8, 8)
    )

    rows = groups.rows["prom:2"]
    assert groups.added_rows == 2
    assert [row[COLUMNS.advertising - 1] for row in rows] == [10, ""]
    assert all(
        row[COLUMNS.operational_date - 1] == sheet_serial(date(2026, 8, 8)) for row in rows
    )


def test_shipped_order_is_grouped_on_shipping_day_without_completion_marker() -> None:
    order = Order(
        source="rozetka",
        external_id="902000001",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        completed_at=datetime(2026, 8, 10, tzinfo=UTC),
        customer_name="Покупець Тестовий",
        city="Київ",
        phone="+380501234567",
        tracking_number="RMP-123456789",
        total=Decimal(1200),
        payment_method="наложка",
        note="пред 400",
        sender="",
        source_status="Відправлено",
        items=[OrderItem("Товар", "608037110", Decimal(1), Decimal(1200), Decimal(1200))],
    )

    groups = collect_order_groups(
        [], [order], {}, sender_default="наш", observation_day=date(2026, 8, 10)
    )

    row = groups.rows["rozetka:902000001"][0]
    assert row[COLUMNS.prepayment - 1] == 400
    assert row[COLUMNS.operational_date - 1] == sheet_serial(date(2026, 8, 10))
    assert row[COLUMNS.first_seen_completed - 1] == ""
    assert row[COLUMNS.order_status - 1] == "Відправлено"


def test_refused_existing_order_is_removed_from_groups() -> None:
    refused_row = [""] * LAST_COLUMN
    refused_row[COLUMNS.source - 1] = "prom"
    refused_row[COLUMNS.tracking_number - 1] = "20451234567890"
    refused_row[COLUMNS.shipment_status - 1] = "Відмова від отримання"
    refused_row[COLUMNS.order_date - 1] = sheet_serial(date(2026, 8, 5))
    refused_row[COLUMNS.sync_key - 1] = "prom:refused"
    refused_row[COLUMNS.row_type - 1] = ROW_ORDER
    refused_row[COLUMNS.operational_date - 1] = sheet_serial(date(2026, 8, 5))
    second_item_row = refused_row.copy()
    second_item_row[COLUMNS.shipment_status - 1] = "Прямує до покупця"

    groups = collect_order_groups(
        [refused_row, second_item_row],
        [],
        {},
        sender_default="наш",
        observation_day=date(2026, 8, 11),
    )

    assert groups.rows == {}


def test_new_order_already_refused_is_not_added() -> None:
    order = Order(
        source="prom",
        external_id="refused",
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
        completed_at=datetime(2026, 8, 11, tzinfo=UTC),
        customer_name="Покупець",
        city="Київ",
        phone="+380501234567",
        tracking_number="20451234567890",
        total=Decimal(100),
        payment_method="наложка",
        note="",
        sender="",
        items=[OrderItem("Товар", "SKU", Decimal(1), Decimal(100), Decimal(100))],
    )
    statuses = {
        "20451234567890": ShipmentStatus(
            "20451234567890", "Відмова від отримання", "102"
        )
    }

    groups = collect_order_groups(
        [], [order], statuses, sender_default="наш", observation_day=date(2026, 8, 11)
    )

    assert groups.rows == {}
    assert groups.added_rows == 0
