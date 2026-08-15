import ast
import inspect
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from crm_sync.clients.google_sheets import GoogleSheetsGateway
from crm_sync.models import Order, OrderAuditEvent, OrderItem, ShipmentStatus
from crm_sync.sheet_layout import ROW_ORDER, sheet_serial
from crm_sync.sheet_schema import COLUMNS, LAST_COLUMN, LAST_COLUMN_LETTER


def test_gateway_has_no_calls_to_removed_private_methods() -> None:
    tree = ast.parse(inspect.getsource(GoogleSheetsGateway))
    methods = {
        node.name
        for node in tree.body[0].body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        and node.func.attr.startswith("_")
    }

    assert called <= methods


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

    def get(self, range_name, **kwargs):
        return self.values

    def __init__(self, values: list[list[Any]]) -> None:
        super().__init__(values)
        self.operations: list[str] = []


class StubSpreadsheet:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def batch_update(self, payload) -> None:
        self.requests.extend(payload["requests"])

    def fetch_sheet_metadata(self, fields):
        return {
            "sheets": [
                {
                    "properties": {"sheetId": LayoutWorksheet.id},
                    "conditionalFormats": [],
                }
            ]
        }


class BackupWorksheet:
    def __init__(self, title: str, sheet_id: int) -> None:
        self.title = title
        self.id = sheet_id


class BackupSpreadsheet(StubSpreadsheet):
    def __init__(self) -> None:
        super().__init__()
        self.copies = [
            BackupWorksheet("_CRM backup - 20260801-000000 - БСК", 10),
            BackupWorksheet("_CRM backup - 20260802-000000 - БСК", 11),
            BackupWorksheet("_CRM backup - 20260803-000000 - БСК", 12),
        ]
        self.deleted: list[str] = []

    def duplicate_sheet(self, *, source_sheet_id: int, new_sheet_name: str):
        duplicate = BackupWorksheet(new_sheet_name, 99)
        self.copies.append(duplicate)
        return duplicate

    def worksheets(self):
        return self.copies

    def del_worksheet(self, worksheet) -> None:
        self.deleted.append(worksheet.title)


def test_refresh_order_details_combines_city_and_recipient_and_restores_markup_formula() -> None:
    rows = [[""] * LAST_COLUMN for _ in range(5)]
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
    assert changed == 12
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
    assert updates["Z5"] == 10
    assert updates["W5"] > 0


def test_refresh_order_details_repairs_text_unit_price_without_product_code() -> None:
    rows = [[""] * LAST_COLUMN for _ in range(5)]
    row = rows[4]
    row[COLUMNS.row_type - 1] = ROW_ORDER
    row[COLUMNS.sync_key - 1] = "prom:421221060"
    row[COLUMNS.quantity - 1] = 1
    row[COLUMNS.unit_price - 1] = "'1"
    row[COLUMNS.line_total - 1] = 4449
    row[COLUMNS.markup - 1] = "#VALUE!"
    worksheet = StubWorksheet(rows)
    gateway = object.__new__(GoogleSheetsGateway)
    gateway.worksheet = worksheet
    gateway.header_row = 4
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
        items=[
            OrderItem(
                name="Product",
                product_code="",
                quantity=Decimal(1),
                unit_price=Decimal(1),
                line_total=Decimal(4449),
            )
        ],
    )

    gateway.refresh_order_details([order])

    updates = {update["range"]: update["values"][0][0] for update in worksheet.updates}
    assert updates["L5"] == 4449
    assert updates["R5"] == "=(L5-Q5)*K5"


def test_completion_observation_backfills_first_seen_date_and_status() -> None:
    rows = [[""] * LAST_COLUMN for _ in range(5)]
    row = rows[4]
    row[COLUMNS.row_type - 1] = ROW_ORDER
    row[COLUMNS.sync_key - 1] = "prom:1"
    row[COLUMNS.operational_date - 1] = sheet_serial(date(2026, 8, 2))
    worksheet = StubWorksheet(rows)
    gateway = object.__new__(GoogleSheetsGateway)
    gateway.worksheet = worksheet
    order = Order(
        source="prom",
        external_id="1",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        completed_at=datetime(2026, 8, 5, tzinfo=UTC),
        customer_name="Customer",
        city="Kyiv",
        phone="+380501234567",
        tracking_number="20451234567890",
        total=Decimal(100),
        payment_method="наложка",
        note="",
        sender="наш",
        completion_is_exact=False,
        items=[OrderItem("Product", "SKU", Decimal(1), Decimal(100), Decimal(100))],
    )

    events = gateway.record_completion_observations(
        [order],
        observed_at=datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
    )

    updates = {update["range"]: update["values"][0][0] for update in worksheet.updates}
    assert updates["D5"] == sheet_serial(date(2026, 8, 2))
    assert updates["X5"] == sheet_serial(date(2026, 8, 2))
    assert updates["Y5"] == "Виконано"
    assert events == ()


def test_completion_observation_audits_previous_known_status() -> None:
    rows = [[""] * LAST_COLUMN for _ in range(5)]
    row = rows[4]
    row[COLUMNS.row_type - 1] = ROW_ORDER
    row[COLUMNS.sync_key - 1] = "prom:1"
    row[COLUMNS.operational_date - 1] = sheet_serial(date(2026, 8, 5))
    row[COLUMNS.order_status - 1] = "Прийнято"
    worksheet = StubWorksheet(rows)
    gateway = object.__new__(GoogleSheetsGateway)
    gateway.worksheet = worksheet
    order = Order(
        source="prom",
        external_id="1",
        created_at=datetime(2026, 8, 5, tzinfo=UTC),
        completed_at=datetime(2026, 8, 5, tzinfo=UTC),
        customer_name="Customer",
        city="Kyiv",
        phone="+380501234567",
        tracking_number="20451234567890",
        total=Decimal(100),
        payment_method="наложка",
        note="",
        sender="наш",
        items=[OrderItem("Product", "SKU", Decimal(1), Decimal(100), Decimal(100))],
    )

    events = gateway.record_completion_observations(
        [order], observed_at=datetime(2026, 8, 5, 10, 0, tzinfo=UTC)
    )

    assert len(events) == 1
    assert events[0].old_value == "Прийнято"
    assert events[0].new_value == "Виконано"


def test_shipped_order_does_not_set_completion_marker_then_transitions_in_place() -> None:
    rows = [[""] * LAST_COLUMN for _ in range(5)]
    row = rows[4]
    row[COLUMNS.row_type - 1] = ROW_ORDER
    row[COLUMNS.sync_key - 1] = "rozetka:1"
    row[COLUMNS.order_date - 1] = sheet_serial(date(2026, 8, 10))
    row[COLUMNS.operational_date - 1] = sheet_serial(date(2026, 8, 10))
    row[COLUMNS.order_status - 1] = "Відправлено"
    worksheet = StubWorksheet(rows)
    gateway = object.__new__(GoogleSheetsGateway)
    gateway.worksheet = worksheet
    order = Order(
        source="rozetka",
        external_id="1",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        completed_at=datetime(2026, 8, 11, tzinfo=UTC),
        customer_name="Customer",
        city="Kyiv",
        phone="+380501234567",
        tracking_number="RMP-123456789",
        total=Decimal(100),
        payment_method="наложка",
        note="",
        sender="наш",
        source_status="Виконано",
        items=[OrderItem("Product", "SKU", Decimal(1), Decimal(100), Decimal(100))],
    )

    events = gateway.record_completion_observations(
        [order], observed_at=datetime(2026, 8, 11, 10, 0, tzinfo=UTC)
    )

    updates = {update["range"]: update["values"][0][0] for update in worksheet.updates}
    assert updates["D5"] == sheet_serial(date(2026, 8, 11))
    assert updates["X5"] == sheet_serial(date(2026, 8, 11))
    assert updates["Y5"] == "Виконано"
    assert "W5" not in updates
    assert len(events) == 1


def test_refresh_order_details_backfills_numeric_prepayment_without_overwriting_manual_value() -> None:
    rows = [[""] * LAST_COLUMN for _ in range(5)]
    row = rows[4]
    row[COLUMNS.row_type - 1] = ROW_ORDER
    row[COLUMNS.sync_key - 1] = "rozetka:1"
    row[COLUMNS.prepayment - 1] = ""
    worksheet = StubWorksheet(rows)
    gateway = object.__new__(GoogleSheetsGateway)
    gateway.worksheet = worksheet
    order = Order(
        source="rozetka",
        external_id="1",
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
        completed_at=datetime(2026, 8, 10, tzinfo=UTC),
        customer_name="Customer",
        city="Kyiv",
        phone="+380501234567",
        tracking_number="RMP-123456789",
        total=Decimal(100),
        payment_method="смешанная",
        note="предо 400",
        sender="наш",
        source_status="Відправлено",
        items=[OrderItem("Product", "SKU", Decimal(1), Decimal(100), Decimal(100))],
    )

    gateway.refresh_order_details([order])

    updates = {update["range"]: update["values"][0][0] for update in worksheet.updates}
    assert updates["P5"] == 400


def test_completion_backfill_migrates_historical_rows_and_repeated_headers() -> None:
    rows = [[""] * LAST_COLUMN for _ in range(6)]
    rows[3][0:2] = ["Джерело", "ТТН"]
    row = rows[5]
    row[COLUMNS.row_type - 1] = ROW_ORDER
    row[COLUMNS.sync_key - 1] = "prom:old"
    row[COLUMNS.order_date - 1] = "01.07.2026"
    worksheet = StubWorksheet(rows)
    gateway = object.__new__(GoogleSheetsGateway)
    gateway.worksheet = worksheet

    changed = gateway.backfill_completion_state(
        observed_at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    )

    updates = {update["range"]: update["values"][0] for update in worksheet.updates}
    assert changed == 3
    assert updates["U4:AA4"][3:5] == [
        "Перше спостереження виконання",
        "Статус замовлення джерела",
    ]
    assert updates["X6"] == [sheet_serial(date(2026, 7, 1))]
    assert updates["Y6"] == ["Виконано"]


def test_append_orders_rebuilds_compact_sections_with_selection_buttons() -> None:
    rows = [[""] * LAST_COLUMN for _ in range(5)]
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

    new_order = Order(
        source="prom",
        external_id="2",
        created_at=datetime(2026, 8, 3, tzinfo=UTC),
        completed_at=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        customer_name="Новий Отримувач",
        city="Київ",
        phone="+380501234567",
        tracking_number="20450000000002",
        total=Decimal(200),
        payment_method="наложка",
        note="",
        sender="наш",
        items=[OrderItem("Новий товар", "SKU-2", Decimal(1), Decimal(200), Decimal(200))],
    )

    added = gateway.append_orders(
        [new_order],
        {},
        sender_default="наш",
        operational_day=date(2026, 8, 3),
        observed_at=datetime(2026, 8, 3, 12, 5, tzinfo=UTC),
    )

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
    assert "↓ До кінця" in written[0][3]
    assert "edit?gid=123#gid=123" in written[0][3]
    assert f"range=A{len(written)}" in written[0][3]
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
    assert worksheet.cleared_ranges == [
        f"A{len(written) + 1}:{LAST_COLUMN_LETTER}{worksheet.row_count}"
    ]
    assert added == 1


def test_professional_formatting_keeps_top_navigation_row_compact() -> None:
    rows = [[""] * LAST_COLUMN]
    rows[0][COLUMNS.row_type - 1] = "MONTH"
    worksheet = LayoutWorksheet(rows)
    spreadsheet = StubSpreadsheet()
    gateway = object.__new__(GoogleSheetsGateway)
    gateway.worksheet = worksheet
    gateway.spreadsheet = spreadsheet
    gateway.header_row = 4

    gateway._apply_professional_formatting(1)

    top_row_formats = [
        request["repeatCell"]
        for request in spreadsheet.requests
        if "repeatCell" in request
        and request["repeatCell"]["range"].get("startRowIndex") == 0
        and request["repeatCell"]["range"].get("endRowIndex") == 1
        and request["repeatCell"]["range"].get("startColumnIndex") == 0
        and request["repeatCell"]["range"].get("endColumnIndex") == 4
    ]
    assert top_row_formats[-1]["cell"]["userEnteredFormat"]["textFormat"]["fontSize"] == 8
    row_heights = [
        request["updateDimensionProperties"]
        for request in spreadsheet.requests
        if "updateDimensionProperties" in request
        and request["updateDimensionProperties"]["range"].get("dimension") == "ROWS"
        and request["updateDimensionProperties"]["range"].get("startIndex") == 0
        and request["updateDimensionProperties"]["range"].get("endIndex") == 1
    ]
    assert row_heights[-1]["properties"]["pixelSize"] == 24


def test_structural_backup_is_hidden_and_retains_only_three_latest_copies() -> None:
    gateway = object.__new__(GoogleSheetsGateway)
    gateway.worksheet = BackupWorksheet("БСК", 123)
    gateway.spreadsheet = BackupSpreadsheet()

    title = gateway.create_backup(created_at=datetime(2026, 8, 5, 12, 30, tzinfo=UTC))

    assert title == "_CRM backup - 20260805-123000 - БСК"
    request = gateway.spreadsheet.requests[0]["updateSheetProperties"]
    assert request["properties"] == {"sheetId": 99, "hidden": True}
    assert gateway.spreadsheet.deleted == ["_CRM backup - 20260801-000000 - БСК"]


def test_append_orders_skips_sheet_rebuild_when_there_are_no_new_orders() -> None:
    worksheet = LayoutWorksheet([[""] * LAST_COLUMN for _ in range(5)])
    gateway = object.__new__(GoogleSheetsGateway)
    gateway.worksheet = worksheet

    added = gateway.append_orders([], {}, sender_default="наш", operational_day=date(2026, 8, 3))

    assert added == 0
    assert worksheet.operations == []


def test_append_orders_advances_layout_when_rebuild_is_forced() -> None:
    rows = [[""] * LAST_COLUMN for _ in range(5)]
    old = rows[4]
    old[COLUMNS.source - 1] = "prom"
    old[COLUMNS.tracking_number - 1] = "20451234567890"
    old[COLUMNS.order_date - 1] = sheet_serial(date(2026, 8, 5))
    old[COLUMNS.order_number - 1] = "1"
    old[COLUMNS.sync_key - 1] = "prom:1"
    old[COLUMNS.row_type - 1] = ROW_ORDER
    old[COLUMNS.operational_date - 1] = sheet_serial(date(2026, 8, 5))
    worksheet = LayoutWorksheet(rows)
    gateway = object.__new__(GoogleSheetsGateway)
    gateway.worksheet = worksheet
    gateway.spreadsheet = StubSpreadsheet()
    gateway.header_row = 4
    gateway._apply_professional_formatting = lambda last_used_row: None

    added = gateway.append_orders(
        [],
        {},
        sender_default="наш",
        operational_day=date(2026, 8, 6),
        force_rebuild=True,
    )

    assert added == 0
    assert any(
        row[COLUMNS.row_type - 1] == "DAY"
        and row[COLUMNS.operational_date - 1] == sheet_serial(date(2026, 8, 6))
        for row in worksheet.written_values
    )
    assert any(row[COLUMNS.row_type - 1] == "REPORT_DAY" for row in worksheet.written_values)


def test_append_orders_does_not_invent_completion_date_for_inexact_source() -> None:
    rows = [[""] * LAST_COLUMN for _ in range(4)]
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
    assert order_row[COLUMNS.order_date - 1] == sheet_serial(date(2026, 8, 3))
    assert order_row[COLUMNS.operational_date - 1] == sheet_serial(date(2026, 8, 3))
    assert order_row[COLUMNS.first_seen_completed - 1] == sheet_serial(date(2026, 8, 3))
    assert order_row[COLUMNS.order_status - 1] == "Виконано"


def test_update_order_expenses_writes_net_total_only_to_first_item_row() -> None:
    rows = [[""] * LAST_COLUMN for _ in range(6)]
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
    assert changed == 3
    assert updates == {"S5": 183.42, "S6": "", "Z5": 183.42}


def test_sheet_integrity_rejects_formula_errors_negative_cost_and_split_order() -> None:
    rows = [[""] * LAST_COLUMN for _ in range(8)]
    for index in (4, 6):
        rows[index][COLUMNS.row_type - 1] = ROW_ORDER
        rows[index][COLUMNS.sync_key - 1] = "prom:501"
        rows[index][COLUMNS.order_date - 1] = "05.08.2026"
    rows[4][COLUMNS.cost - 1] = "-10"
    rows[6][COLUMNS.markup - 1] = "#REF!"
    worksheet = StubWorksheet(rows)
    gateway = object.__new__(GoogleSheetsGateway)
    gateway.worksheet = worksheet

    report = gateway.validate_integrity()

    assert not report.ok
    assert any("negative unit cost" in error for error in report.errors)
    assert any("formula error" in error for error in report.errors)
    assert any("split across" in error for error in report.errors)


def test_shipment_status_updates_create_one_audit_change_per_order() -> None:
    rows = [[""] * LAST_COLUMN for _ in range(6)]
    for index in (4, 5):
        rows[index][COLUMNS.row_type - 1] = ROW_ORDER
        rows[index][COLUMNS.source - 1] = "🟣 Prom"
        rows[index][COLUMNS.sync_key - 1] = "prom:501"
        rows[index][COLUMNS.tracking_number - 1] = "20451234567890"
        rows[index][COLUMNS.shipment_status - 1] = "Відправлення у дорозі"
    rows[4][COLUMNS.order_number - 1] = "501"
    worksheet = StubWorksheet(rows)
    gateway = object.__new__(GoogleSheetsGateway)
    gateway.worksheet = worksheet

    result = gateway.update_shipment_statuses(
        {
            "20451234567890": ShipmentStatus(
                tracking_number="20451234567890",
                status="Отримано",
            )
        }
    )

    assert result.cell_updates == 2
    assert len(result.changes) == 1
    assert result.changes[0].order_id == "501"
    assert result.changes[0].old_status == "Відправлення у дорозі"
    assert result.changes[0].new_status == "Отримано"


class AuditWorksheetStub:
    def __init__(self) -> None:
        self.header: list[Any] = []
        self.appended: list[list[Any]] = []

    def row_values(self, row: int):
        return self.header

    def update(self, *, values, range_name, raw) -> None:
        self.header = values[0]

    def freeze(self, *, rows: int) -> None:
        self.frozen_rows = rows

    def format(self, range_name, cell_format) -> None:
        self.formatted_range = range_name

    def append_rows(self, rows, *, value_input_option) -> None:
        self.appended.extend(rows)


class HealthWorksheetStub(AuditWorksheetStub):
    id = 555

    def __init__(self, values=None) -> None:
        super().__init__()
        self.values = values or []

    def get_all_values(self):
        return self.values


class AuditSpreadsheetStub:
    def __init__(self) -> None:
        self.audit = AuditWorksheetStub()

    def worksheet(self, title: str):
        from gspread.exceptions import WorksheetNotFound

        if not self.audit.header:
            raise WorksheetNotFound(title)
        return self.audit

    def add_worksheet(self, *, title: str, rows: int, cols: int):
        return self.audit


def test_audit_log_creates_technical_sheet_and_appends_event() -> None:
    gateway = object.__new__(GoogleSheetsGateway)
    gateway.spreadsheet = AuditSpreadsheetStub()
    event = OrderAuditEvent(
        occurred_at=datetime(2026, 8, 5, 14, 30, tzinfo=UTC),
        event_type="Додано замовлення",
        source="prom",
        order_id="501",
        sync_key="prom:501",
        tracking_number="20451234567890",
        new_value="Додано до CRM",
    )

    written = gateway.append_audit_events([event])

    audit = gateway.spreadsheet.audit
    assert written == 1
    assert audit.header[0:2] == ["Час", "Подія"]
    assert audit.appended[0][1:6] == [
        "Додано замовлення",
        "🟣 Prom",
        "501",
        "prom:501",
        "20451234567890",
    ]


def test_health_state_triggers_once_on_third_failure_and_recovers() -> None:
    gateway = object.__new__(GoogleSheetsGateway)
    health = HealthWorksheetStub(
        [["consecutive_failures", "2"], ["alert_open", "false"]]
    )

    class Spreadsheet:
        def worksheet(self, title):
            return health

    gateway.spreadsheet = Spreadsheet()

    failed = gateway.record_sync_health(
        ["prom"], occurred_at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    )

    assert failed.consecutive_failures == 3
    assert failed.alert_due
    health.values = [["consecutive_failures", "3"], ["alert_open", "true"]]

    recovered = gateway.record_sync_health(
        [], occurred_at=datetime(2026, 8, 5, 12, 15, tzinfo=UTC)
    )

    assert recovered.consecutive_failures == 0
    assert recovered.recovered
