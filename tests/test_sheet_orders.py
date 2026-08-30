from datetime import UTC, date, datetime
from decimal import Decimal

from crm_sync.models import Order, OrderItem, ShipmentStatus
from crm_sync.sheet_layout import REPORTING_EXCLUDED_REFUSAL, ROW_ORDER, sheet_serial
from crm_sync.sheet_orders import collect_order_groups, markup_formula, net_profit_formula
from crm_sync.sheet_schema import COLUMNS, LAST_COLUMN


def test_markup_formula_uses_ukrainian_sheet_separator_and_numeric_safe_text() -> None:
    assert markup_formula(5) == (
        '=IF(OR(Q5="";LOWER(Q5&"")="предоплата");L5*K5;'
        'IF(ISNUMBER(Q5);(L5-Q5)*K5;""))'
    )


def test_net_profit_formula_requires_numeric_cost_and_subtracts_all_expenses() -> None:
    assert net_profit_formula(5) == (
        '=IF(AND(ISNUMBER(Q5);ISNUMBER(R5));'
        'R5-IFERROR(AA5;0)-IFERROR(AC5;0);"")'
    )


def test_collect_order_groups_normalizes_legacy_rows_without_losing_manual_values() -> None:
    row = [""] * LAST_COLUMN
    row[COLUMNS.source - 1] = "prom"
    row[COLUMNS.tracking_number - 1] = "2045 1234 5678 90"
    row[COLUMNS.order_date - 1] = "05.08.2026 12:30"
    row[COLUMNS.customer - 1] = "Київ, Петренко Іван Іванович"
    row[COLUMNS.sender - 1] = "-"
    row[COLUMNS.cost - 1] = 700
    row[COLUMNS.receipt - 1] = "https://check.checkbox.ua/receipt/abc"
    row[COLUMNS.installment_commission - 1] = 49.17
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
    assert normalized[COLUMNS.receipt - 1] == "https://check.checkbox.ua/receipt/abc"
    assert normalized[COLUMNS.installment_commission_source - 1] == "legacy"


def test_collect_order_groups_migrates_legacy_melad_sender() -> None:
    row = [""] * LAST_COLUMN
    row[COLUMNS.tracking_number - 1] = "20451234567890"
    row[COLUMNS.order_date - 1] = "05.08.2026"
    row[COLUMNS.sender - 1] = "Melad"
    row[COLUMNS.sync_key - 1] = "prom:melad"
    row[COLUMNS.row_type - 1] = ROW_ORDER
    row[COLUMNS.operational_date - 1] = sheet_serial(date(2026, 8, 5))

    groups = collect_order_groups(
        [row], [], {}, sender_default="наш", observation_day=date(2026, 8, 8)
    )

    assert groups.rows["prom:melad"][0][COLUMNS.sender - 1] == "Melad дроп"


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
        installment_commission=Decimal("3.70"),
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
    assert rows[0][COLUMNS.advertising - 1] == "10.00\n3.70"
    assert rows[0][COLUMNS.advertising_base - 1] == 10
    assert rows[0][COLUMNS.installment_commission - 1] == 3.7
    assert rows[0][COLUMNS.installment_commission_source - 1] == "reported"
    assert rows[1][COLUMNS.advertising - 1] == ""
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


def test_explicit_order_prepayment_is_written_without_note_text() -> None:
    order = Order(
        source="prom",
        external_id="prepaid",
        created_at=datetime(2026, 8, 13, tzinfo=UTC),
        completed_at=datetime(2026, 8, 13, tzinfo=UTC),
        customer_name="Покупець",
        city="Київ",
        phone="+380501234567",
        tracking_number="20451510462545",
        total=Decimal(4449),
        payment_method="смешанная",
        note="",
        sender="наш",
        prepayment=Decimal(1000),
        items=[OrderItem("Товар", "SKU", Decimal(1), Decimal(4449), Decimal(4449))],
    )

    groups = collect_order_groups(
        [], [order], {}, sender_default="наш", observation_day=date(2026, 8, 13)
    )

    assert groups.rows["prom:prepaid"][0][COLUMNS.prepayment - 1] == 1000


def test_refused_existing_order_is_removed_from_groups() -> None:
    refused_row = [""] * LAST_COLUMN
    refused_row[COLUMNS.source - 1] = "prom"
    refused_row[COLUMNS.tracking_number - 1] = "20451234567890"
    refused_row[COLUMNS.shipment_status - 1] = "Відмова від отримання"
    refused_row[COLUMNS.order_date - 1] = sheet_serial(date(2026, 8, 5))
    refused_row[COLUMNS.sync_key - 1] = "prom:refused"
    refused_row[COLUMNS.row_type - 1] = ROW_ORDER
    refused_row[COLUMNS.operational_date - 1] = sheet_serial(date(2026, 8, 5))
    refused_row[COLUMNS.reporting_state - 1] = REPORTING_EXCLUDED_REFUSAL
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


def test_refused_existing_multi_item_order_with_prepayment_is_retained_and_excluded() -> None:
    first = [""] * LAST_COLUMN
    first[COLUMNS.source - 1] = "prom"
    first[COLUMNS.tracking_number - 1] = "20451234567890"
    first[COLUMNS.shipment_status - 1] = "Відмова від отримання"
    first[COLUMNS.order_date - 1] = sheet_serial(date(2026, 8, 5))
    first[COLUMNS.prepayment - 1] = 300
    first[COLUMNS.sync_key - 1] = "prom:prepaid-refusal"
    first[COLUMNS.row_type - 1] = ROW_ORDER
    first[COLUMNS.operational_date - 1] = sheet_serial(date(2026, 8, 5))
    second = first.copy()
    second[COLUMNS.prepayment - 1] = ""
    second[COLUMNS.shipment_status - 1] = "Прямує до покупця"

    groups = collect_order_groups(
        [first, second], [], {}, sender_default="наш", observation_day=date(2026, 8, 11)
    )

    rows = groups.rows["prom:prepaid-refusal"]
    assert len(rows) == 2
    assert all(
        row[COLUMNS.reporting_state - 1] == REPORTING_EXCLUDED_REFUSAL for row in rows
    )


def test_supplier_prepayment_marker_preserves_refused_order() -> None:
    row = [""] * LAST_COLUMN
    row[COLUMNS.source - 1] = "prom"
    row[COLUMNS.tracking_number - 1] = "20451234567890"
    row[COLUMNS.shipment_status - 1] = "Відмова від отримання"
    row[COLUMNS.order_date - 1] = sheet_serial(date(2026, 8, 5))
    row[COLUMNS.cost - 1] = "Предоплата"
    row[COLUMNS.sync_key - 1] = "prom:supplier-prepaid"
    row[COLUMNS.row_type - 1] = ROW_ORDER
    row[COLUMNS.operational_date - 1] = sheet_serial(date(2026, 8, 5))

    groups = collect_order_groups(
        [row], [], {}, sender_default="наш", observation_day=date(2026, 8, 11)
    )

    retained = groups.rows["prom:supplier-prepaid"][0]
    assert retained[COLUMNS.reporting_state - 1] == REPORTING_EXCLUDED_REFUSAL


def test_resolved_supplier_prepayment_preserves_matching_refusal() -> None:
    row = [""] * LAST_COLUMN
    row[COLUMNS.source - 1] = "prom"
    row[COLUMNS.tracking_number - 1] = "20451234567890"
    row[COLUMNS.shipment_status - 1] = "Відмова від отримання"
    row[COLUMNS.order_date - 1] = sheet_serial(date(2026, 8, 5))
    row[COLUMNS.sync_key - 1] = "prom:unverified-refusal"
    row[COLUMNS.row_type - 1] = ROW_ORDER
    row[COLUMNS.operational_date - 1] = sheet_serial(date(2026, 8, 5))

    groups = collect_order_groups(
        [row],
        [],
        {},
        sender_default="наш",
        observation_day=date(2026, 8, 11),
        supplier_prepayment_tracking_keys={"20451234567890"},
    )

    retained = groups.rows["prom:unverified-refusal"][0]
    assert retained[COLUMNS.reporting_state - 1] == REPORTING_EXCLUDED_REFUSAL


def test_supplier_outage_quarantines_refusal_until_it_can_be_verified() -> None:
    row = [""] * LAST_COLUMN
    row[COLUMNS.source - 1] = "prom"
    row[COLUMNS.tracking_number - 1] = "20451234567890"
    row[COLUMNS.shipment_status - 1] = "Відмова від отримання"
    row[COLUMNS.order_date - 1] = sheet_serial(date(2026, 8, 5))
    row[COLUMNS.sync_key - 1] = "prom:unverified-refusal"
    row[COLUMNS.row_type - 1] = ROW_ORDER
    row[COLUMNS.operational_date - 1] = sheet_serial(date(2026, 8, 5))

    groups = collect_order_groups(
        [row],
        [],
        {},
        sender_default="наш",
        observation_day=date(2026, 8, 11),
        quarantine_unverified_refusals=True,
    )

    retained = groups.rows["prom:unverified-refusal"][0]
    assert retained[COLUMNS.reporting_state - 1] == REPORTING_EXCLUDED_REFUSAL


def test_new_refused_order_with_note_prepayment_is_retained_and_excluded() -> None:
    order = Order(
        source="prom",
        external_id="refused-prepaid",
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
        completed_at=datetime(2026, 8, 11, tzinfo=UTC),
        customer_name="Покупець",
        city="Київ",
        phone="+380501234567",
        tracking_number="20451234567890",
        total=Decimal(100),
        payment_method="смешанная",
        note="Предоплата 50 грн",
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

    row = groups.rows["prom:refused-prepaid"][0]
    assert row[COLUMNS.prepayment - 1] == 50
    assert row[COLUMNS.reporting_state - 1] == REPORTING_EXCLUDED_REFUSAL
