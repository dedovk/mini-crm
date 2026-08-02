from __future__ import annotations

import ast
import calendar
import re
from datetime import date, datetime, timedelta
from typing import Any

BUSINESS_HEADERS = (
    "Джерело",
    "ТТН",
    "Статус доставки (Нова пошта)",
    "Дата замовлення",
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
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


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
        11: f"=COUNTUNIQUEIFS({range_for('U')};{order_filter};{day_filter})",
        14: f"=SUMIFS({range_for('N')};{order_filter};{day_filter})",
        17: (
            f"=SUMPRODUCT(({range_for('V')}=\"{ROW_ORDER}\")*"
            f"({range_for('W')}={day_expr})*IFERROR({range_for('K')}*{range_for('Q')};0))"
        ),
        18: f"=SUMIFS({range_for('R')};{order_filter};{day_filter})",
        19: f"=SUMIFS({range_for('S')};{order_filter};{day_filter})",
    }
    mtd = {
        11: f"=COUNTUNIQUEIFS({range_for('U')};{order_filter};{mtd_filter})",
        14: f"=SUMIFS({range_for('N')};{order_filter};{mtd_filter})",
        17: (
            f"=SUMPRODUCT(({range_for('V')}=\"{ROW_ORDER}\")*"
            f"({range_for('W')}>={month_start})*({range_for('W')}<={day_expr})*"
            f"IFERROR({range_for('K')}*{range_for('Q')};0))"
        ),
        18: f"=SUMIFS({range_for('R')};{order_filter};{mtd_filter})",
        19: f"=SUMIFS({range_for('S')};{order_filter};{mtd_filter})",
    }
    forecast = {}
    for column, formula in mtd.items():
        projected = f"{formula[1:]}*{days_in_month}/{elapsed}"
        forecast[column] = f"=ROUNDUP({projected};0)" if column == 11 else f"={projected}"
    return {ROW_REPORT_DAY: daily, ROW_REPORT_MTD: mtd, ROW_REPORT_FORECAST: forecast}


def clean_customer_display(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw.startswith("{"):
        return raw
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
    return ", ".join(part for part in (city, customer) if part) or raw
