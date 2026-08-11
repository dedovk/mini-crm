from datetime import date

from crm_sync.sheet_layout import (
    ROW_REPORT_DAY,
    ROW_REPORT_FORECAST,
    clean_customer_display,
    month_period_label,
    parse_order_day,
    parse_sheet_date,
    report_formulas,
    sheet_serial,
    source_display,
    source_key,
)


def test_sheet_dates_and_month_label() -> None:
    day = date(2026, 8, 3)

    assert parse_sheet_date(sheet_serial(day)) == day
    assert parse_sheet_date(0.5) is None
    assert parse_sheet_date("03.08.2026") == day
    assert month_period_label(day) == "01.08.2026 — 31.08.2026"
    assert parse_order_day(sheet_serial(day) + 0.5) == day


def test_report_formulas_filter_only_order_rows_and_operational_day() -> None:
    formulas = report_formulas(date(2026, 8, 2), first_data_row=5, last_data_row=30)

    assert '$V$5:$V$30;"ORDER"' in formulas[ROW_REPORT_DAY][4]
    assert "DATE(2026;8;2)" in formulas[ROW_REPORT_DAY][6]
    assert formulas[ROW_REPORT_FORECAST][4].startswith("=ROUNDUP(")
    assert formulas[ROW_REPORT_FORECAST][6].endswith(")*31/2")
    assert formulas[ROW_REPORT_FORECAST][8].startswith("=(SUMIFS(")
    assert ")*31/2" in formulas[ROW_REPORT_FORECAST][8]
    assert formulas[ROW_REPORT_DAY][8].startswith("=SUMIFS($M$5:$M$30;")
    assert "-SUMIFS($R$5:$R$30;" in formulas[ROW_REPORT_DAY][8]
    assert '$A$5:$A$30;"*Prom*";$S$5:$S$30;"<>10"' in formulas[ROW_REPORT_DAY][12]
    assert '$A$5:$A$30;"*Rozetka*"' in formulas[ROW_REPORT_DAY][14]
    assert '$A$5:$A$30;"*Prom*";$S$5:$S$30;10' in formulas[ROW_REPORT_DAY][16]
    assert 12 not in formulas[ROW_REPORT_FORECAST]
    assert 14 not in formulas[ROW_REPORT_FORECAST]
    assert 16 not in formulas[ROW_REPORT_FORECAST]


def test_clean_customer_display_removes_serialized_rozetka_city() -> None:
    value = "{'id': 330, 'name': 'Київ', 'name_ua': 'Київ'}, Тестовий Клієнт По-батькові"

    assert clean_customer_display(value) == "Київ, Тестовий Клієнт"


def test_source_icons_are_stable_and_reversible() -> None:
    assert source_display("prom") == "🟣 Prom"
    assert source_display("Rozetka") == "🟢 Rozetka"
    assert source_display("opencart") == "🔴 IBOX-SHOP"
    assert source_display("phone") == "🔵 Телефон"
    assert source_key("🟢 Rozetka") == "rozetka"
    assert source_key("🔴 IBOX-SHOP") == "opencart"
    assert source_key("🔵 Телефон") == "opencart"
