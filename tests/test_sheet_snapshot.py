from datetime import date

from crm_sync.sheet_layout import ROW_ORDER, ROW_REPORT_DAY
from crm_sync.sheet_orders import OrderGroups
from crm_sync.sheet_schema import COLUMNS, LAST_COLUMN
from crm_sync.sheet_snapshot import build_sheet_snapshot


def test_rebuild_preserves_receipt_and_counts_multi_item_installment_once() -> None:
    first = [""] * LAST_COLUMN
    first[COLUMNS.row_type - 1] = ROW_ORDER
    first[COLUMNS.receipt - 1] = "https://check.checkbox.ua/receipt/abc"
    first[COLUMNS.installment_commission - 1] = 49.17
    second = [""] * LAST_COLUMN
    second[COLUMNS.row_type - 1] = ROW_ORDER
    groups = OrderGroups(
        rows={"prom:1": [first, second]},
        days={"prom:1": date(2026, 8, 15)},
        sort_values={"prom:1": "1"},
    )

    snapshot = build_sheet_snapshot(
        groups,
        operational_day=date(2026, 8, 16),
        sheet_id=1,
        spreadsheet_id="sheet",
    )

    order_rows = [row for row in snapshot.rows if row[COLUMNS.row_type - 1] == ROW_ORDER]
    report = next(
        row for row in snapshot.rows if row[COLUMNS.row_type - 1] == ROW_REPORT_DAY
    )
    assert order_rows[0][COLUMNS.receipt - 1] == "https://check.checkbox.ua/receipt/abc"
    assert [row[COLUMNS.installment_commission - 1] for row in order_rows] == [49.17, ""]
    assert "$AB$" in report[17]
