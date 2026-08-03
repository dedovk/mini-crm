from __future__ import annotations

import ast
import calendar
import re
from datetime import date, timedelta
from typing import Any

from crm_sync.utils import customer_display, short_person_name

BUSINESS_HEADERS = (
    "Джерело",
    "ТТН",
    "Статус доставки (Нова пошта)",
    "Час виконання",
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

TECHNICAL_HEADERS = ("Sync Key", "Тип рядка", "Операційна дата")
ALL_HEADERS = BUSINESS_HEADERS + TECHNICAL_HEADERS

ROW_ORDER = "ORDER"
ROW_HEADER = "HEADER"
ROW_DAY = "DAY"
ROW_MONTH = "MONTH"
ROW_REPORT_DAY = "REPORT_DAY"
ROW_REPORT_MTD = "REPORT_MTD"
ROW_REPORT_FORECAST = "REPORT_FORECAST"

EXCEL_EPOCH = date(1899, 12, 30)


def sheet_serial(value: date) -> int:
    return (value - EXCEL_EPOCH).days


def parse_sheet_date(value: Any) -> date | None:
    if isinstance(value, (int, float)):
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


def parse_sheet_time(value: Any) -> str:
    if isinstance(value, (int, float)):
        total_minutes = round((float(value) % 1) * 24 * 60) % (24 * 60)
        hours, minutes = divmod(total_minutes, 60)
        return f"{hours:02d}:{minutes:02d}"
    raw = str(value or "").strip()
    match = re.search(r"(?:^|\s)(\d{2}:\d{2})(?::\d{2})?$", raw)
    return match.group(1) if match else ""


def month_period_label(day: date) -> str:
    last_day = calendar.monthrange(day.year, day.month)[1]
    return f"01.{day:%m.%Y} — {last_day:02d}.{day:%m.%Y}"


def report_formulas(day: date, *, first_data_row: int, last_data_row: int) -> dict[str, dict[int, str]]:
    start = max(1, first_data_row)
    end = max(start, last_data_row)
    day_expr = f"DATE({day.year};{day.month};{day.day})"
    month_start = f"DATE({day.year};{day.month};1)"
    range_for = lambda column: f"${column}${start}:${column}${end}"
    order_filter = f'{range_for("V")};"{ROW_ORDER}"'
    day_filter = f"{range_for('W')};{day_expr}"
    mtd_filter = f'{range_for("W")};">="&{month_start};{range_for("W")};"<="&{day_expr}'
    elapsed = day.day
    days_in_month = calendar.monthrange(day.year, day.month)[1]

    daily = {
        4: f"=COUNTUNIQUEIFS({range_for('U')};{order_filter};{day_filter})",
        6: f"=SUMIFS({range_for('N')};{order_filter};{day_filter})",
        8: (
            f"=SUMIFS({range_for('M')};{order_filter};{day_filter})-"
            f"SUMIFS({range_for('R')};{order_filter};{day_filter})"
        ),
        10: f"=SUMIFS({range_for('R')};{order_filter};{day_filter})",
        12: f"=SUMIFS({range_for('S')};{order_filter};{day_filter})",
    }
    mtd = {
        4: f"=COUNTUNIQUEIFS({range_for('U')};{order_filter};{mtd_filter})",
        6: f"=SUMIFS({range_for('N')};{order_filter};{mtd_filter})",
        8: (
            f"=SUMIFS({range_for('M')};{order_filter};{mtd_filter})-"
            f"SUMIFS({range_for('R')};{order_filter};{mtd_filter})"
        ),
        10: f"=SUMIFS({range_for('R')};{order_filter};{mtd_filter})",
        12: f"=SUMIFS({range_for('S')};{order_filter};{mtd_filter})",
    }
    forecast = {}
    for column, formula in mtd.items():
        projected = f"{formula[1:]}*{days_in_month}/{elapsed}"
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
