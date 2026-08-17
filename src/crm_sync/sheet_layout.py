from __future__ import annotations

import ast
import calendar
import re
from datetime import date, timedelta
from typing import Any

from gspread.utils import rowcol_to_a1

from crm_sync.sheet_schema import COLUMNS
from crm_sync.utils import customer_display, short_person_name

BUSINESS_HEADERS = (
    "Джерело",
    "ТТН",
    "Статус доставки (Нова пошта)",
    "Дата виконання",
    "№ замовлення",
    "Місто / отримувач",
    "Телефон",
    "Товар",
    "Код товару",
    "Відправник",
    "Кількість",
    "Ціна за одиницю, грн",
    "Сума товарної позиції, грн",
    "Сума замовлення, грн",
    "Спосіб оплати",
    "Передоплата, грн",
    "Собівартість за одиницю, грн",
    "Націнка, грн",
    "Витрати на рекламу, грн",
    "Примітка менеджера",
)

TECHNICAL_HEADERS = (
    "Sync Key",
    "Тип рядка",
    "Операційна дата",
    "Перше спостереження виконання",
    "Статус замовлення джерела",
    "Базові витрати маркетплейсу, грн",
    "Чек",
    "Комісія оплати частинами, грн",
    "Джерело комісії оплати частинами",
    "Стан звітності",
)
ALL_HEADERS = BUSINESS_HEADERS + TECHNICAL_HEADERS

ROW_ORDER = "ORDER"
ROW_HEADER = "HEADER"
ROW_DAY = "DAY"
ROW_MONTH = "MONTH"
ROW_REPORT_DAY = "REPORT_DAY"
ROW_REPORT_MTD = "REPORT_MTD"
ROW_REPORT_FORECAST = "REPORT_FORECAST"
REPORTING_EXCLUDED_REFUSAL = "EXCLUDED_REFUSAL"

EXCEL_EPOCH = date(1899, 12, 30)

SOURCE_LABELS = {
    "prom": "🟣 Prom",
    "rozetka": "🟢 Rozetka",
    "opencart": "🔴 IBOX-SHOP",
    "site": "🔴 IBOX-SHOP",
    "сайт": "🔴 IBOX-SHOP",
}


def source_key(value: Any) -> str:
    raw = str(value or "").strip()
    normalized = re.sub(r"^[^\wА-Яа-яІіЇїЄєҐґ]+", "", raw).strip().casefold()
    aliases = {
        "prom": "prom",
        "rozetka": "rozetka",
        "сайт": "opencart",
        "site": "opencart",
        "opencart": "opencart",
        "ibox-shop": "opencart",
        "ibox shop": "opencart",
        "phone": "opencart",
        "телефон": "opencart",
    }
    return aliases.get(normalized, normalized)


def source_display(value: Any) -> str:
    raw = str(value or "").strip()
    if raw.casefold() in {"phone", "телефон"} or "телефон" in raw.casefold():
        return "🔵 Телефон"
    key = source_key(value)
    if not key:
        return ""
    return SOURCE_LABELS.get(key, f"⚪ {str(value).strip()}")


def sheet_serial(value: date) -> int:
    return (value - EXCEL_EPOCH).days


def parse_sheet_date(value: Any) -> date | None:
    if isinstance(value, (int, float)):
        if float(value) < 1:
            return None
        return EXCEL_EPOCH + timedelta(days=int(value))
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if "-" in raw:
            return date.fromisoformat(raw)
        separator = "." if "." in raw else "/"
        day, month, year = (int(part) for part in raw.split(separator))
        return date(year, month, day)
    except (TypeError, ValueError):
        pass
    return None


def parse_order_day(value: Any) -> date | None:
    if isinstance(value, (int, float)):
        return parse_sheet_date(value)
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return parse_sheet_date(float(raw))
    except ValueError:
        pass
    return parse_sheet_date(raw[:10])


def month_period_label(day: date) -> str:
    last_day = calendar.monthrange(day.year, day.month)[1]
    return f"01.{day:%m.%Y} — {last_day:02d}.{day:%m.%Y}"


def report_formulas(day: date, *, first_data_row: int, last_data_row: int) -> dict[str, dict[int, str]]:
    start = max(1, first_data_row)
    end = max(start, last_data_row)
    day_expr = f"DATE({day.year};{day.month};{day.day})"
    month_start = f"DATE({day.year};{day.month};1)"
    def column_letter(column: int) -> str:
        return re.sub(r"\d", "", rowcol_to_a1(1, column))

    def range_for(column: int) -> str:
        letter = column_letter(column)
        return f"${letter}${start}:${letter}${end}"
    order_filter = (
        f'{range_for(COLUMNS.row_type)};"{ROW_ORDER}";'
        f'{range_for(COLUMNS.reporting_state)};"<>{REPORTING_EXCLUDED_REFUSAL}"'
    )
    day_filter = f"{range_for(COLUMNS.operational_date)};{day_expr}"
    mtd_filter = (
        f'{range_for(COLUMNS.operational_date)};">="&{month_start};'
        f'{range_for(COLUMNS.operational_date)};"<="&{day_expr}'
    )
    source_range = range_for(COLUMNS.source)
    advertising_range = range_for(COLUMNS.advertising_base)
    installment_range = range_for(COLUMNS.installment_commission)
    elapsed = day.day
    days_in_month = calendar.monthrange(day.year, day.month)[1]

    def advertising_formula(period_filter: str, category: str) -> str:
        source_criterion = "*Rozetka*" if category == "rozetka" else "*Prom*"
        amount_filter = (
            f";{advertising_range};10"
            if category == "prom_fixed"
            else f';{advertising_range};"<>10"'
            if category == "prosale"
            else ""
        )
        base = (
            f"=SUMIFS({advertising_range};{order_filter};{period_filter};"
            f'{source_range};"{source_criterion}"{amount_filter})'
        )
        return base

    daily = {
        4: f"=COUNTUNIQUEIFS({range_for(COLUMNS.sync_key)};{order_filter};{day_filter})",
        6: f"=SUMIFS({range_for(COLUMNS.order_total)};{order_filter};{day_filter})",
        8: (
            f"=SUMIFS({range_for(COLUMNS.line_total)};{order_filter};{day_filter})-"
            f"SUMIFS({range_for(COLUMNS.markup)};{order_filter};{day_filter})"
        ),
        10: f"=SUMIFS({range_for(COLUMNS.markup)};{order_filter};{day_filter})",
        12: advertising_formula(day_filter, "prosale"),
        14: advertising_formula(day_filter, "rozetka"),
        16: advertising_formula(day_filter, "prom_fixed"),
        18: f"=SUMIFS({installment_range};{order_filter};{day_filter})",
    }
    mtd = {
        4: f"=COUNTUNIQUEIFS({range_for(COLUMNS.sync_key)};{order_filter};{mtd_filter})",
        6: f"=SUMIFS({range_for(COLUMNS.order_total)};{order_filter};{mtd_filter})",
        8: (
            f"=SUMIFS({range_for(COLUMNS.line_total)};{order_filter};{mtd_filter})-"
            f"SUMIFS({range_for(COLUMNS.markup)};{order_filter};{mtd_filter})"
        ),
        10: f"=SUMIFS({range_for(COLUMNS.markup)};{order_filter};{mtd_filter})",
        12: advertising_formula(mtd_filter, "prosale"),
        14: advertising_formula(mtd_filter, "rozetka"),
        16: advertising_formula(mtd_filter, "prom_fixed"),
        18: f"=SUMIFS({installment_range};{order_filter};{mtd_filter})",
    }
    forecast = {}
    for column, formula in mtd.items():
        if column in {12, 14, 16, 18}:
            continue
        projected = f"({formula[1:]})*{days_in_month}/{elapsed}"
        forecast[column] = f"=ROUNDUP({projected};0)" if column == 4 else f"={projected}"
    return {ROW_REPORT_DAY: daily, ROW_REPORT_MTD: mtd, ROW_REPORT_FORECAST: forecast}


def clean_customer_display(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw.startswith("{"):
        city, separator, name = raw.partition(",")
        return customer_display(city, name) if separator else short_person_name(raw)
    city_blob, separator, customer = raw.partition("},")
    city_blob = city_blob + ("}" if separator else "")
    city = ""
    try:
        parsed = ast.literal_eval(city_blob)
        if isinstance(parsed, dict):
            city = str(parsed.get("name_ua") or parsed.get("name") or parsed.get("title") or "").strip()
    except (SyntaxError, ValueError):
        match = re.search(r"['\"](?:name_ua|name|title)['\"]\s*:\s*['\"]([^'\"]+)", city_blob)
        city = match.group(1).strip() if match else ""
    customer = customer.strip(" ,")
    return customer_display(city, customer) or raw
