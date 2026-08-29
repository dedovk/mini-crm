from datetime import date

from crm_sync.sheet_layout import ROW_DAY, ROW_ORDER, ROW_REPORT_DAY
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
        daily_usd_rates={date(2026, 8, 15): "45,20"},
    )

    order_rows = [row for row in snapshot.rows if row[COLUMNS.row_type - 1] == ROW_ORDER]
    report = next(
        row for row in snapshot.rows if row[COLUMNS.row_type - 1] == ROW_REPORT_DAY
    )
    assert order_rows[0][COLUMNS.receipt - 1] == "https://check.checkbox.ua/receipt/abc"
    assert [row[COLUMNS.installment_commission - 1] for row in order_rows] == [49.17, ""]
    assert "$AB$" in report[17]
    day_rows = [row for row in snapshot.rows if row[COLUMNS.row_type - 1] == ROW_DAY]
    assert day_rows[0][3:5] == ["Курс USD", "45,20"]
    assert day_rows[1][3:5] == ["Курс USD", ""]


def test_every_historical_day_across_months_has_an_independent_usd_rate_cell() -> None:
    july_row = [""] * LAST_COLUMN
    july_row[COLUMNS.row_type - 1] = ROW_ORDER
    august_row = [""] * LAST_COLUMN
    august_row[COLUMNS.row_type - 1] = ROW_ORDER
    groups = OrderGroups(
        rows={"prom:july": [july_row], "prom:august": [august_row]},
        days={
            "prom:july": date(2026, 7, 31),
            "prom:august": date(2026, 8, 29),
        },
        sort_values={"prom:july": "1", "prom:august": "2"},
    )

    snapshot = build_sheet_snapshot(
        groups,
        operational_day=date(2026, 8, 29),
        sheet_id=1,
        spreadsheet_id="sheet",
        daily_usd_rates={
            date(2026, 7, 31): "44,80",
            date(2026, 8, 1): "44,90",
        },
    )

    day_rows = [row for row in snapshot.rows if row[COLUMNS.row_type - 1] == ROW_DAY]
    assert len(day_rows) == 30
    assert all(row[3] == "Курс USD" for row in day_rows)
    assert day_rows[0][4] == "44,80"
    assert day_rows[1][4] == "44,90"
    assert day_rows[-1][4] == ""


def test_historical_rate_keeps_its_day_even_after_all_orders_for_day_are_removed() -> None:
    august_row = [""] * LAST_COLUMN
    august_row[COLUMNS.row_type - 1] = ROW_ORDER
    groups = OrderGroups(
        rows={"prom:august": [august_row]},
        days={"prom:august": date(2026, 8, 29)},
        sort_values={"prom:august": "1"},
    )

    snapshot = build_sheet_snapshot(
        groups,
        operational_day=date(2026, 8, 29),
        sheet_id=1,
        spreadsheet_id="sheet",
        daily_usd_rates={date(2026, 7, 31): "44,80"},
    )

    day_rows = [row for row in snapshot.rows if row[COLUMNS.row_type - 1] == ROW_DAY]
    assert len(day_rows) == 30
    assert day_rows[0][1] == 46234
    assert day_rows[0][3:5] == ["Курс USD", "44,80"]
