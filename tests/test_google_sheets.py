from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from crm_sync.clients.google_sheets import COLUMNS, GoogleSheetsGateway
from crm_sync.models import Order, OrderItem
from crm_sync.sheet_layout import ROW_ORDER, sheet_serial


class StubWorksheet:
    def __init__(self, values: list[list[Any]]) -> None:
        self.values = values
        self.updates: list[dict] = []

    def get_all_values(self, **kwargs):
        return self.values

    def batch_update(self, updates, **kwargs) -> None:
        self.updates = updates


class LayoutWorksheet(StubWorksheet):
    id = 123
    row_count = 1000

    def batch_clear(self, ranges) -> None:
        self.operations.append("clear")
        self.cleared_ranges = ranges

    def update(self, *, values, range_name, raw) -> None:
        self.operations.append("update")
        self.written_values = values
        self.written_range = range_name
        self.written_raw = raw

    def add_rows(self, count) -> None:
        self.row_count += count

    def __init__(self, values: list[list[Any]]) -> None:
        super().__init__(values)
        self.operations: list[str] = []


class StubSpreadsheet:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def batch_update(self, payload) -> None:
        self.requests.extend(payload["requests"])


def test_refresh_order_details_combines_city_and_recipient_and_restores_markup_formula() -> None:
    rows = [[""] * COLUMNS.operational_date for _ in range(5)]
    rows[4][COLUMNS.row_type - 1] = ROW_ORDER
    rows[4][COLUMNS.sync_key - 1] = "prom:1"
    rows[4][COLUMNS.tracking_number - 1] = 20451501572223
    worksheet = StubWorksheet(rows)
    gateway = object.__new__(GoogleSheetsGateway)
    gateway.worksheet = worksheet
    gateway.header_row = 4
    order = Order(
        source="prom",
        external_id="1",
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
        completed_at=datetime(2026, 8, 3, 12, 34, tzinfo=UTC),
        customer_name="Тестовий Отримувач",
        city="Київ",
        phone="+380501234567",
        tracking_number="RMP-483122083",
        total=Decimal(100),
        payment_method="",
        note="",
        sender="",
        items=[
            OrderItem(
                name="Товар",
                product_code="608037110",
                quantity=Decimal(1),
                unit_price=Decimal(100),
                line_total=Decimal(100),
            )
        ],
        advertising_cost=Decimal(10),
    )

    changed = gateway.refresh_order_details([order])

    updates = {update["range"]: update["values"][0][0] for update in worksheet.updates}
    assert changed == 11
    assert updates["B5"] == "RMP-483122083"
    assert updates["D5"] == sheet_serial(date(2026, 8, 3))
    assert updates["F5"] == "Київ, Тестовий Отримувач"
    assert updates["I5"] == "608037110"
    assert updates["K5"] == 1
    assert updates["L5"] == 100
    assert updates["M5"] == 100
    assert updates["N5"] == 100
    assert updates["R5"] == "=(L5-Q5)*K5"
    assert updates["S5"] == 10
    assert updates["W5"] > 0


def test_append_orders_rebuilds_compact_sections_with_selection_buttons() -> None:
    rows = [[""] * COLUMNS.operational_date for _ in range(5)]
    old = rows[4]
    old[COLUMNS.source - 1] = "prom"
    old[COLUMNS.tracking_number - 1] = "20451234567890"
    old[COLUMNS.order_date - 1] = "01.07.2026 10:00"
    old[COLUMNS.order_number - 1] = "1"
    old[COLUMNS.customer - 1] = "Київ, Прізвище Ім'я По-батькові"
    old[COLUMNS.sync_key - 1] = "prom:1"
    old[COLUMNS.row_type - 1] = ROW_ORDER
    old[COLUMNS.operational_date - 1] = sheet_serial(date(2026, 7, 1))
    old[COLUMNS.sender - 1] = "-"

    worksheet = LayoutWorksheet(rows)
    spreadsheet = StubSpreadsheet()
    gateway = object.__new__(GoogleSheetsGateway)
    gateway.worksheet = worksheet
    gateway.spreadsheet = spreadsheet
    gateway.header_row = 4
    gateway._apply_professional_formatting = lambda last_used_row: None

    added = gateway.append_orders([], {}, sender_default="наш", operational_day=date(2026, 8, 3))

    written = worksheet.written_values
    order_row = next(row for row in written if row[COLUMNS.row_type - 1] == ROW_ORDER)
    assert order_row[COLUMNS.operational_date - 1] != date(2026, 8, 3)
    assert order_row[COLUMNS.source - 1] == "🟣 Prom"
    assert order_row[COLUMNS.customer - 1] == "Київ, Прізвище Ім'я"
    assert order_row[COLUMNS.order_date - 1] == sheet_serial(date(2026, 7, 1))
    assert order_row[COLUMNS.sender - 1] == "наш"
    assert not any(not any(str(value).strip() for value in row) for row in written)
    month_rows = [row for row in written if row[COLUMNS.row_type - 1] == "MONTH"]
    day_rows = [row for row in written if row[COLUMNS.row_type - 1] == "DAY"]
    assert all("Виділити місяць" in row[2] for row in month_rows)
    assert all("Виділити день" in row[2] for row in day_rows)
    report_indexes = [
        index
        for index, row in enumerate(written)
        if row[COLUMNS.row_type - 1] in {"REPORT_DAY", "REPORT_MTD", "REPORT_FORECAST"}
    ]
    assert written[report_indexes[0] + 1][COLUMNS.row_type - 1] == "REPORT_MTD"
    assert written[report_indexes[1] + 1][COLUMNS.row_type - 1] == "REPORT_FORECAST"
    report_row = written[report_indexes[0]]
    assert report_row[10] == "ProSale, грн"
    assert report_row[12] == "Rozetka, грн"
    assert report_row[14] == "Prom 10 грн"
    assert "<>10" in report_row[11]
    assert "*Rozetka*" in report_row[13]
    assert "*Prom*" in report_row[15]
    assert report_row[15].endswith(";10)")
    forecast_row = written[report_indexes[2]]
    assert forecast_row[10:16] == ["", "", "", "", "", ""]
    assert worksheet.operations == ["update", "clear"]
    assert worksheet.cleared_ranges == [f"A{len(written) + 1}:W{worksheet.row_count}"]
    assert added == 0


def test_append_orders_does_not_invent_completion_date_for_inexact_source() -> None:
    rows = [[""] * COLUMNS.operational_date for _ in range(4)]
    worksheet = LayoutWorksheet(rows)
    spreadsheet = StubSpreadsheet()
    gateway = object.__new__(GoogleSheetsGateway)
    gateway.worksheet = worksheet
    gateway.spreadsheet = spreadsheet
    gateway.header_row = 4
    gateway._apply_professional_formatting = lambda last_used_row: None
    order = Order(
        source="prom",
        external_id="2",
        created_at=datetime(2026, 8, 2, tzinfo=UTC),
        completed_at=datetime(2026, 8, 3, tzinfo=UTC),
        customer_name="Test Customer",
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
                product_code="SKU-2",
                quantity=Decimal(1),
                unit_price=Decimal(100),
                line_total=Decimal(100),
            )
        ],
    )

    gateway.append_orders([order], {}, sender_default="наш", operational_day=date(2026, 8, 3))

    order_row = next(
        row for row in worksheet.written_values if row[COLUMNS.row_type - 1] == ROW_ORDER
    )
    assert order_row[COLUMNS.order_date - 1] == ""
    assert order_row[COLUMNS.operational_date - 1] == sheet_serial(date(2026, 8, 3))


def test_update_order_expenses_writes_net_total_only_to_first_item_row() -> None:
    rows = [[""] * COLUMNS.operational_date for _ in range(6)]
    for index in (4, 5):
        rows[index][COLUMNS.row_type - 1] = ROW_ORDER
        rows[index][COLUMNS.sync_key - 1] = "rozetka:901"
        rows[index][COLUMNS.source - 1] = "🟢 Rozetka"
        rows[index][COLUMNS.advertising - 1] = "99" if index == 4 else "15"
    worksheet = StubWorksheet(rows)
    gateway = object.__new__(GoogleSheetsGateway)
    gateway.worksheet = worksheet

    changed = gateway.update_order_expenses({"901": Decimal("183.42")}, source="rozetka")

    updates = {update["range"]: update["values"][0][0] for update in worksheet.updates}
    assert changed == 2
    assert updates == {"S5": 183.42, "S6": ""}
