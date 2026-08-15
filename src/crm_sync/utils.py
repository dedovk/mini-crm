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
MEEST_TRACKING_RE = re.compile(
    r"\b(?:MEEST-)?(?:\d{3}|[A-ZА-ЯІЇЄ]{3})-\d{6,9}\b",
    re.IGNORECASE,
)
_PREPAYMENT_WORD = (
    r"(?:"
    r"перед(?:о?пл(?:ат)?\w*)?"
    r"|пред(?:(?:о?пл(?:ат)?\w*)|о)?"
    r"|аванс\w*|завдат\w*|задат\w*"
    r"|частков\w*\s+оплат\w*|частичн\w*\s+оплат\w*"
    r"|pered(?:o?pl(?:at)?\w*)?|pred(?:(?:o?pl(?:at)?\w*)|o)?|avans\w*"
    r")"
)
_PREPAYMENT_AMOUNT = r"(?P<amount>\d[\d\s\u00a0]*(?:[.,]\d{1,2})?)"
_PREPAYMENT_WITH_ABBREVIATION = rf"(?:{_PREPAYMENT_WORD}|п\s*[/.-]\s*о)"

PREPAYMENT_MARKER_RE = re.compile(rf"\b{_PREPAYMENT_WORD}\b", re.IGNORECASE)
NEGATIVE_PREPAYMENT_RE = re.compile(
    rf"(?:"
    rf"\b(?:без|немає|нет|не\s+треба|не\s+потрібн\w*|не\s+нужн\w*)\s+{_PREPAYMENT_WORD}\b"
    rf"|\b{_PREPAYMENT_WORD}\s+(?:не\s+треба|не\s+потрібн\w*|не\s+нужн\w*|не\s+буде)\b"
    rf")",
    re.IGNORECASE,
)
PREPAYMENT_AFTER_RE = re.compile(
    rf"\b{_PREPAYMENT_WITH_ABBREVIATION}\b\s*[-–—:=,.]?\s*"
    rf"(?:(?:сума|сумма|у\s+розмірі|в\s+размере)\s*[-:=]?\s*)?"
    rf"{_PREPAYMENT_AMOUNT}",
    re.IGNORECASE,
)
PREPAYMENT_BEFORE_RE = re.compile(
    rf"{_PREPAYMENT_AMOUNT}\s*(?:грн\.?|₴|uah)?\s*[-–—:=,.]?\s*"
    rf"\b{_PREPAYMENT_WITH_ABBREVIATION}\b",
    re.IGNORECASE,
)


def decimal_value(value: Any, default: Decimal = Decimal(0)) -> Decimal:
    if value is None or value == "":
        return default
    if isinstance(value, dict):
        return decimal_value(first_value(value, "amount", "value", "price"), default)
    normalized = str(value).replace("\u00a0", "").replace(" ", "").replace(",", ".")
    match = re.search(r"-?\d+(?:\.\d+)?", normalized)
    if not match:
        return default
    try:
        return Decimal(match.group(0))
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


def person_name(value: Any) -> str:
    """Return a readable person name without leaking serialized API objects."""
    if not isinstance(value, dict):
        return str(value or "").strip()
    for key in ("full_name", "title"):
        candidate = value.get(key)
        if candidate and not isinstance(candidate, (dict, list, tuple, set)):
            return str(candidate).strip()
    return " ".join(
        str(value.get(key)).strip()
        for key in ("last_name", "surname", "first_name", "second_name", "patronymic")
        if value.get(key) and not isinstance(value.get(key), (dict, list, tuple, set))
    )


def short_person_name(value: Any) -> str:
    """Keep only surname and first name for the operational sheet."""
    normalized = person_name(value)
    return " ".join(normalized.replace(",", " ").split()[:2])


def customer_display(city: Any, customer_name: Any) -> str:
    parts = [display_text(city), short_person_name(customer_name)]
    return ", ".join(dict.fromkeys(part for part in parts if part))


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
    for pattern in (RMP_RE, INTERNATIONAL_TRACKING_RE, TTN_RE, UKRPOST_DOMESTIC_RE, MEEST_TRACKING_RE):
        match = pattern.search(raw)
        if match:
            return " ".join(match.group(0).split()).strip(" ,;.")
    return ""


def find_tracking_number(*values: Any) -> str:
    for value in values:
        normalized = normalize_tracking_number(value)
        if normalized:
            return normalized
    return ""


def parse_prepayment(note: str) -> Decimal:
    # Marketplace histories can contain an old "без передоплати" entry and a
    # newer positive seller note. Remove only negative phrases instead of
    # rejecting the whole combined history.
    text = NEGATIVE_PREPAYMENT_RE.sub("", note or "")
    matches = sorted(
        [*PREPAYMENT_AFTER_RE.finditer(text), *PREPAYMENT_BEFORE_RE.finditer(text)],
        key=lambda match: match.start(),
    )
    if matches:
        return decimal_value(matches[-1].group("amount"))
    return Decimal(0)


def has_prepayment_request(note: str) -> bool:
    text = note or ""
    without_negative_statements = NEGATIVE_PREPAYMENT_RE.sub("", text)
    return bool(PREPAYMENT_MARKER_RE.search(without_negative_statements))


def collect_note_text(value: Any) -> list[str]:
    """Collect note-like API text, including undocumented nested seller comments."""
    result: list[str] = []

    def visit(current: Any, *, note_context: bool = False) -> None:
        if isinstance(current, dict):
            for key, nested in current.items():
                key_name = str(key).casefold()
                nested_note_context = note_context or any(
                    marker in key_name
                    for marker in ("note", "comment", "remark", "примітк", "примеч")
                )
                visit(nested, note_context=nested_note_context)
        elif isinstance(current, (list, tuple)):
            for nested in current:
                visit(nested, note_context=note_context)
        elif isinstance(current, str):
            text = current.strip()
            if text and (note_context or PREPAYMENT_MARKER_RE.search(text)):
                result.append(text)

    visit(value)
    return list(dict.fromkeys(result))


def classify_payment(raw_method: str, note: str) -> str:
    raw = f"{raw_method} {note}".casefold()
    has_prepayment = parse_prepayment(note) > 0 or has_prepayment_request(note)
    if has_prepayment:
        return "смешанная"
    if any(word in raw for word in ("зачет", "залік", "взаємозалік", "offset", "credit note")):
        return "Зачет"
    if "част" in raw or "credit" in raw:
        return "оплата частями"
    if any(word in raw for word in ("счет", "рахун", "invoice", "bank transfer")):
        return "оплата на счет"
    if any(word in raw for word in ("налож", "cod", "cash on delivery", "післяплат", "послеплат")):
        return "наложка"
    if any(
        word in raw
        for word in (
            "prom", "карт", "card", "apple pay", "applepay", "google pay", "googlepay",
            "visa", "mastercard", "liqpay", "wayforpay", "online", "онлайн",
        )
    ):
        return "пром оплата(оплата картой)"
    normalized = raw_method.strip()
    return normalized if normalized in {
        "пром оплата(оплата картой)", "оплата частями", "наложка",
        "оплата на счет", "смешанная", "Зачет",
    } else "наложка"


def normalize_shipment_status(value: Any, status_code: Any = "") -> str:
    """Collapse in-transit carrier states into one stable CRM value."""
    status = str(value or "").strip() or "Невідомо"
    normalized = status.casefold().replace("’", "'")
    refusal_terms = (
        "відмова від отримання",
        "відмовився від отримання",
        "відмовилась від отримання",
        "відмовився від посилки",
        "отказ от получения",
        "отказался от получения",
        "отказалась от получения",
    )
    if any(term in normalized for term in refusal_terms):
        return "Відмова від отримання"
    transit_terms = (
        "у дороз",
        "в дороз",
        "прямує",
        "прибуло у відділення",
        "прибуло до відділення",
        "прибыло в отделение",
        "передано кур'єру",
        "передано курьеру",
        "кур'єр отримав",
        "курьер получил",
        "очікує у поштоматі",
        "ожидает в почтомате",
        "прийнято у відділенні",
        "принято в отделении",
        "відправлено у місто",
        "відправлено до міста",
        "отправлено в город",
        "знаходиться в місті одержувача",
    )
    if any(term in normalized for term in transit_terms):
        return "Прямує до покупця"
    return status


def is_refused_shipment_status(value: Any) -> bool:
    return normalize_shipment_status(value).casefold() == "відмова від отримання"


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


def parse_optional_datetime(value: Any, timezone: str) -> datetime | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return parse_datetime(value, timezone)
    except (TypeError, ValueError):
        return None


def decimal_for_sheet(value: Decimal) -> int | float:
    integral = value.to_integral_value()
    return int(integral) if value == integral else float(value)
