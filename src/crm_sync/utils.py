from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

TTN_RE = re.compile(r"(?<!\d)((?:\d[\s-]*){14})(?!\d)")
RMP_RE = re.compile(r"\bRMP-\d+\b", re.IGNORECASE)
INTERNATIONAL_TRACKING_RE = re.compile(r"\b[A-Z]{2}\d{9}[A-Z]{2}\b", re.IGNORECASE)
UKRPOST_DOMESTIC_RE = re.compile(r"(?<!\d)\d{13}(?!\d)")
HYPHENATED_TRACKING_RE = re.compile(r"\b[A-Z0-9]{2,}(?:-[A-Z0-9]{2,})+\b", re.IGNORECASE)
PREPAYMENT_RE = re.compile(
    r"(?:\bперед\b|\bпредоплат\w*\b)\s*[-:=]?\s*(\d[\d\s]*(?:[.,]\d{1,2})?)",
    re.IGNORECASE,
)


def decimal_value(value: Any, default: Decimal = Decimal(0)) -> Decimal:
    if value is None or value == "":
        return default
    normalized = str(value).replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        return Decimal(normalized)
    except (InvalidOperation, ValueError):
        return default


def first_value(mapping: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return default


def nested_value(mapping: dict[str, Any], paths: Iterable[tuple[str, ...]], default: Any = "") -> Any:
    for path in paths:
        current: Any = mapping
        for key in path:
            if not isinstance(current, dict) or key not in current:
                break
            current = current[key]
        else:
            if current is not None and current != "":
                return current
    return default


def display_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(first_value(value, "name_ua", "name", "title", "region_title", default="")).strip()
    return str(value or "").strip()


def city_from_address(value: Any) -> str:
    raw = display_text(value)
    if not raw:
        return ""
    match = re.search(r"(?:^|,\s*)(?:м\.|г\.|с\.|смт\.?|село)\s*([^,]+)", raw, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return raw.split(",", 1)[0].strip()


def normalize_phone(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    digits = digits.removeprefix("00")
    if len(digits) == 10 and digits.startswith("0"):
        digits = "38" + digits
    elif len(digits) == 9:
        digits = "380" + digits
    elif digits.startswith("80") and len(digits) == 11:
        digits = "3" + digits
    return f"+{digits}" if digits else ""


def extract_ttn(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        match = TTN_RE.search(str(value))
        if match:
            digits = re.sub(r"\D", "", match.group(1))
            if len(digits) == 14:
                return digits
    return ""


def normalize_tracking_number(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    raw = " ".join(str(value).replace("\u00a0", " ").split()).strip()
    if not raw:
        return ""
    raw = re.sub(
        r"^(?:ттн|ttn|ен\s*№|ен|декларац(?:ія|ии)|номер\s+(?:ттн|накладної))\s*[:№#-]?\s*",
        "",
        raw,
        flags=re.IGNORECASE,
    )
    for pattern in (RMP_RE, INTERNATIONAL_TRACKING_RE, TTN_RE, UKRPOST_DOMESTIC_RE, HYPHENATED_TRACKING_RE):
        match = pattern.search(raw)
        if match:
            return " ".join(match.group(0).split()).strip(" ,;.")
    if re.fullmatch(r"[A-ZА-ЯІЇЄ0-9-]{6,64}", raw, re.IGNORECASE) and re.search(r"\d", raw):
        return raw.strip(" ,;.")
    return ""


def find_tracking_number(*values: Any) -> str:
    for value in values:
        normalized = normalize_tracking_number(value)
        if normalized:
            return normalized
    return ""


def parse_prepayment(note: str) -> Decimal:
    match = PREPAYMENT_RE.search(note or "")
    return decimal_value(match.group(1)) if match else Decimal(0)


def classify_payment(raw_method: str, note: str) -> str:
    raw = f"{raw_method} {note}".casefold()
    prepayment = parse_prepayment(note)
    if prepayment > 0 and any(word in raw for word in ("налож", "cod", "післяплат", "послеплат")):
        return "смешанная"
    if "част" in raw or "credit" in raw:
        return "оплата частями"
    if any(word in raw for word in ("счет", "рахун", "invoice", "bank transfer")):
        return "оплата на счет"
    if any(word in raw for word in ("налож", "cod", "cash on delivery", "післяплат", "послеплат")):
        return "наложка"
    if any(word in raw for word in ("prom", "карт", "card", "liqpay", "wayforpay", "online")):
        return "пром оплата(оплата картой)"
    if prepayment > 0:
        return "смешанная"
    return raw_method.strip()


def parse_datetime(value: Any, timezone: str) -> datetime:
    zone = ZoneInfo(timezone)
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip().replace("Z", "+00:00")
        parsed = None
        for candidate in (raw, raw.replace("/", "-")):
            try:
                parsed = datetime.fromisoformat(candidate)
                break
            except ValueError:
                pass
        if parsed is None:
            for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(raw, fmt).replace(tzinfo=zone)
                    break
                except ValueError:
                    pass
        if parsed is None:
            raise ValueError(f"Unsupported order date: {value!r}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def decimal_for_sheet(value: Decimal) -> int | float:
    integral = value.to_integral_value()
    return int(integral) if value == integral else float(value)
