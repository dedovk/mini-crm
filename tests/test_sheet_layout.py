from datetime import date

from crm_sync.sheet_layout import (
    ROW_REPORT_DAY,
    ROW_REPORT_FORECAST,
    clean_customer_display,
    month_period_label,
    parse_order_day,
    parse_sheet_date,
    parse_sheet_time,
    report_formulas,
    sheet_serial,
)


def test_sheet_dates_and_month_label() -> None:
    day = date(2026, 8, 3)

    assert parse_sheet_date(sheet_serial(day)) == day
    assert parse_sheet_date("03.08.2026") == day
    assert month_period_label(day) == "01.08.2026 — 31.08.2026"
    assert parse_order_day(sheet_serial(day) + 0.5) == day
    assert parse_sheet_time(0.5) == "12:00"
    assert parse_sheet_time(sheet_serial(day) + (9 * 60 + 4) / (24 * 60)) == "09:04"
    assert parse_sheet_time("03.08.2026 14:25") == "14:25"


def test_report_formulas_filter_only_order_rows_and_operational_day() -> None:
    formulas = report_formulas(date(2026, 8, 2), first_data_row=5, last_data_row=30)

    assert '$V$5:$V$30;"ORDER"' in formulas[ROW_REPORT_DAY][4]
    assert "DATE(2026;8;2)" in formulas[ROW_REPORT_DAY][6]
    assert formulas[ROW_REPORT_FORECAST][4].startswith("=ROUNDUP(")
    assert formulas[ROW_REPORT_FORECAST][6].endswith("*31/2")
    assert formulas[ROW_REPORT_DAY][8].startswith("=SUMIFS($M$5:$M$30;")
    assert "-SUMIFS($R$5:$R$30;" in formulas[ROW_REPORT_DAY][8]


def test_clean_customer_display_removes_serialized_rozetka_city() -> None:
    value = "{'id': 330, 'name': 'Київ', 'name_ua': 'Київ'}, Тестовий Клієнт По-батькові"

    assert clean_customer_display(value) == "Київ, Тестовий Клієнт"
