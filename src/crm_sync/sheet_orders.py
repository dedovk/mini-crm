from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from gspread.utils import rowcol_to_a1

from crm_sync.models import Order, ShipmentStatus
from crm_sync.sheet_layout import (
    REPORTING_EXCLUDED_REFUSAL,
    ROW_ORDER,
    clean_customer_display,
    parse_order_day,
    parse_sheet_date,
    sheet_serial,
    source_display,
    source_key,
)
from crm_sync.sheet_schema import COLUMNS, LAST_COLUMN
from crm_sync.supplier_identity import MELAD_LEGACY_SENDERS, MELAD_SENDER
from crm_sync.utils import (
    customer_display,
    decimal_for_sheet,
    decimal_value,
    extract_ttn,
    has_prepayment_request,
    is_refused_shipment_status,
    normalize_shipment_status,
    normalize_tracking_number,
    parse_prepayment,
    tracking_match_key,
)

_REFUSED_ORDER_STATUS_MARKERS = (
    "відмова",
    "отказ",
    "скас",
    "отмен",
    "cancel",
    "refus",
)


def is_refused_order_status(value: Any) -> bool:
    """Return whether a marketplace status represents cancellation or refusal."""
    normalized = str(value or "").strip().casefold()
    if normalized.startswith(("не скас", "не отмен", "not cancel", "not refus")):
        return False
    return any(marker in normalized for marker in _REFUSED_ORDER_STATUS_MARKERS)


def row_has_prepayment(row: list[Any]) -> bool:
    """Detect every persisted prepayment signal used by the CRM."""
    prepayment = row[COLUMNS.prepayment - 1] if len(row) >= COLUMNS.prepayment else ""
    cost = row[COLUMNS.cost - 1] if len(row) >= COLUMNS.cost else ""
    note = row[COLUMNS.manager_note - 1] if len(row) >= COLUMNS.manager_note else ""
    payment = row[COLUMNS.payment_method - 1] if len(row) >= COLUMNS.payment_method else ""
    return (
        decimal_value(prepayment) > 0
        or has_prepayment_request(str(cost or ""))
        or has_prepayment_request(str(note or ""))
        or str(payment).strip().casefold() == "смешанная"
    )


def row_is_refused(row: list[Any]) -> bool:
    """Detect refusal from either Nova Poshta or the source order status."""
    shipment = row[COLUMNS.shipment_status - 1] if len(row) >= COLUMNS.shipment_status else ""
    order_status = row[COLUMNS.order_status - 1] if len(row) >= COLUMNS.order_status else ""
    return is_refused_shipment_status(shipment) or is_refused_order_status(order_status)


def markup_formula(row_number: int) -> str:
    """Build a margin formula that tolerates supplier text markers."""
    unit_price = rowcol_to_a1(row_number, COLUMNS.unit_price)
    cost = rowcol_to_a1(row_number, COLUMNS.cost)
    quantity = rowcol_to_a1(row_number, COLUMNS.quantity)
    return (
        f'=IF(OR({cost}="";LOWER({cost}&"")="предоплата");'
        f'{unit_price}*{quantity};IF(ISNUMBER({cost});'
        f'({unit_price}-{cost})*{quantity};""))'
    )


def net_profit_formula(row_number: int) -> str:
    """Build net profit from margin and both marketplace commission components."""
    cost = rowcol_to_a1(row_number, COLUMNS.cost)
    markup = rowcol_to_a1(row_number, COLUMNS.markup)
    advertising = rowcol_to_a1(row_number, COLUMNS.advertising_base)
    installment = rowcol_to_a1(row_number, COLUMNS.installment_commission)
    return (
        f'=IF(AND(ISNUMBER({cost});ISNUMBER({markup}));'
        f'{markup}-IFERROR({advertising};0)-IFERROR({installment};0);"")'
    )


def advertising_display(base: Decimal, installment: Decimal) -> Any:
    """Return a compact two-line display while numeric components stay hidden."""
    if installment > 0:
        return (
            f"{decimal_for_sheet(base):.2f}\n"
            f"{decimal_for_sheet(installment):.2f}"
        )
    return decimal_for_sheet(base) if base > 0 else ""


@dataclass(slots=True)
class OrderGroups:
    rows: dict[str, list[list[Any]]]
    days: dict[str, date]
    sort_values: dict[str, str]
    added_rows: int = 0


def collect_order_groups(
    existing_values: list[list[Any]],
    orders: list[Order],
    shipment_statuses: dict[str, ShipmentStatus],
    *,
    sender_default: str,
    observation_day: date,
    excluded_sync_keys: set[str] | None = None,
    supplier_prepayment_tracking_keys: set[str] | None = None,
    quarantine_unverified_refusals: bool = False,
) -> OrderGroups:
    result = OrderGroups(rows={}, days={}, sort_values={})
    excluded = {key.strip().casefold() for key in (excluded_sync_keys or set())}
    supplier_prepayments = supplier_prepayment_tracking_keys or set()
    order_rows = [
        row
        for row in existing_values
        if len(row) >= COLUMNS.sync_key
        and str(row[COLUMNS.row_type - 1]).strip() == ROW_ORDER
    ]
    refused_keys = {
        str(row[COLUMNS.sync_key - 1]).strip().casefold()
        for row in order_rows
        if row_is_refused(row)
    } | excluded
    prepaid_keys = {
        str(row[COLUMNS.sync_key - 1]).strip().casefold()
        for row in order_rows
        if row_has_prepayment(row)
    }
    prepaid_keys.update(
        str(row[COLUMNS.sync_key - 1]).strip().casefold()
        for row in order_rows
        if tracking_match_key(row[COLUMNS.tracking_number - 1]) in supplier_prepayments
    )
    for source_row in existing_values:
        key = (
            str(source_row[COLUMNS.sync_key - 1]).strip().casefold()
            if len(source_row) >= COLUMNS.sync_key
            else ""
        )
        retained_refusal = key in refused_keys and (
            key in prepaid_keys or quarantine_unverified_refusals
        )
        if key in refused_keys and not retained_refusal:
            continue
        normalized = _normalize_existing_row(
            source_row,
            sender_default=sender_default,
            retain_refusal=retained_refusal,
        )
        if normalized is None:
            continue
        key, order_day, sort_value, row = normalized
        result.rows.setdefault(key, []).append(row)
        result.days[key] = order_day
        result.sort_values[key] = sort_value

    existing_keys = set(result.rows)
    for order in orders:
        key = order.sync_key.casefold()
        if key in existing_keys:
            continue
        rows = _new_order_rows(
            order,
            shipment_statuses,
            sender_default=sender_default,
            observation_day=observation_day,
        )
        if not rows:
            continue
        effective_day = order.completed_at.date() if order.completion_is_exact else observation_day
        result.rows[key] = rows
        result.days[key] = effective_day
        result.sort_values[key] = order.completed_at.strftime("%Y-%m-%d %H:%M")
        result.added_rows += len(rows)
        existing_keys.add(key)
    return result


def _normalize_existing_row(
    source_row: list[Any], *, sender_default: str, retain_refusal: bool = False
) -> tuple[str, date, str, list[Any]] | None:
    row = list(source_row[:LAST_COLUMN]) + [""] * max(0, LAST_COLUMN - len(source_row))
    if str(row[COLUMNS.row_type - 1]).strip() != ROW_ORDER:
        return None
    key = str(row[COLUMNS.sync_key - 1]).strip().casefold()
    order_day = parse_sheet_date(row[COLUMNS.operational_date - 1]) or parse_order_day(
        row[COLUMNS.order_date - 1]
    )
    tracking = normalize_tracking_number(row[COLUMNS.tracking_number - 1])
    if not key or not order_day or not tracking:
        return None

    row[COLUMNS.source - 1] = source_display(row[COLUMNS.source - 1])
    row[COLUMNS.tracking_number - 1] = tracking
    row[COLUMNS.shipment_status - 1] = normalize_shipment_status(
        row[COLUMNS.shipment_status - 1]
    )
    if row_is_refused(row) and not retain_refusal:
        return None
    row[COLUMNS.reporting_state - 1] = (
        REPORTING_EXCLUDED_REFUSAL if retain_refusal else ""
    )
    row[COLUMNS.customer - 1] = clean_customer_display(row[COLUMNS.customer - 1])
    completion_day = parse_order_day(row[COLUMNS.order_date - 1])
    row[COLUMNS.order_date - 1] = sheet_serial(completion_day) if completion_day else ""
    if not str(row[COLUMNS.sender - 1]).strip() or row[COLUMNS.sender - 1] == "-":
        row[COLUMNS.sender - 1] = sender_default
    elif str(row[COLUMNS.sender - 1]).strip().casefold() in MELAD_LEGACY_SENDERS:
        row[COLUMNS.sender - 1] = MELAD_SENDER
    if not str(row[COLUMNS.payment_method - 1]).strip() and source_key(
        row[COLUMNS.source - 1]
    ) == "rozetka":
        row[COLUMNS.payment_method - 1] = "наложка"
    row[COLUMNS.row_type - 1] = ROW_ORDER
    row[COLUMNS.operational_date - 1] = sheet_serial(order_day)
    if not str(row[COLUMNS.order_status - 1]).strip():
        row[COLUMNS.order_status - 1] = "Виконано"
    if (
        str(row[COLUMNS.order_status - 1]).strip().casefold() == "виконано"
        and not parse_sheet_date(row[COLUMNS.first_seen_completed - 1])
    ):
        row[COLUMNS.first_seen_completed - 1] = sheet_serial(completion_day or order_day)
    base = decimal_value(row[COLUMNS.advertising_base - 1])
    installment = decimal_value(row[COLUMNS.installment_commission - 1])
    if installment > 0 and not str(
        row[COLUMNS.installment_commission_source - 1]
    ).strip():
        row[COLUMNS.installment_commission_source - 1] = "legacy"
    if base == 0 and installment == 0:
        # One-time migration from the former numeric business column.
        base = decimal_value(row[COLUMNS.advertising - 1])
        row[COLUMNS.advertising_base - 1] = decimal_for_sheet(base) if base > 0 else ""
    row[COLUMNS.advertising - 1] = advertising_display(base, installment)
    return key, order_day, str(row[COLUMNS.order_date - 1]), row


def _new_order_rows(
    order: Order,
    shipment_statuses: dict[str, ShipmentStatus],
    *,
    sender_default: str,
    observation_day: date,
) -> list[list[Any]]:
    shipment_status = shipment_statuses.get(extract_ttn(order.tracking_number)) or shipment_statuses.get(
        order.tracking_number
    )
    prepayment = order.prepayment or parse_prepayment(order.note)
    refused = bool(
        (shipment_status and is_refused_shipment_status(shipment_status.status))
        or is_refused_order_status(order.source_status)
    )
    has_prepayment = (
        prepayment > 0
        or has_prepayment_request(order.note)
        or order.payment_method.strip().casefold() == "смешанная"
    )
    if refused and not has_prepayment:
        return []
    sender = order.sender.strip() or sender_default
    effective_day = order.completed_at.date() if order.completion_is_exact else observation_day
    rows: list[list[Any]] = []
    for item in order.items:
        row: list[Any] = [""] * LAST_COLUMN
        row[COLUMNS.source - 1] = source_display(order.channel or order.source)
        row[COLUMNS.tracking_number - 1] = order.tracking_number
        row[COLUMNS.shipment_status - 1] = (
            shipment_status.status
            if shipment_status
            else ("Невідомо" if extract_ttn(order.tracking_number) else "Інший перевізник")
        )
        row[COLUMNS.order_date - 1] = sheet_serial(effective_day)
        row[COLUMNS.order_number - 1] = order.external_id
        row[COLUMNS.customer - 1] = customer_display(order.city, order.customer_name)
        row[COLUMNS.phone - 1] = order.phone
        row[COLUMNS.product - 1] = item.name
        row[COLUMNS.product_code - 1] = item.product_code
        row[COLUMNS.sender - 1] = sender
        row[COLUMNS.quantity - 1] = decimal_for_sheet(item.quantity)
        row[COLUMNS.unit_price - 1] = decimal_for_sheet(item.unit_price)
        row[COLUMNS.line_total - 1] = decimal_for_sheet(item.line_total)
        row[COLUMNS.order_total - 1] = decimal_for_sheet(order.total)
        row[COLUMNS.payment_method - 1] = order.payment_method or (
            "наложка" if order.source.casefold() == "rozetka" else ""
        )
        row[COLUMNS.prepayment - 1] = (
            decimal_for_sheet(prepayment) if prepayment > Decimal(0) else ""
        )
        if not rows:
            row[COLUMNS.advertising_base - 1] = (
                decimal_for_sheet(order.advertising_cost) if order.advertising_cost > 0 else ""
            )
            row[COLUMNS.installment_commission - 1] = (
                decimal_for_sheet(order.installment_commission)
                if order.installment_commission > 0
                else ""
            )
            row[COLUMNS.installment_commission_source - 1] = (
                order.installment_commission_source
                or ("reported" if order.installment_commission > 0 else "")
            )
            row[COLUMNS.advertising - 1] = advertising_display(
                order.advertising_cost, order.installment_commission
            )
        row[COLUMNS.sync_key - 1] = order.sync_key
        row[COLUMNS.row_type - 1] = ROW_ORDER
        row[COLUMNS.operational_date - 1] = sheet_serial(effective_day)
        row[COLUMNS.first_seen_completed - 1] = (
            sheet_serial(observation_day) if order.is_completed else ""
        )
        row[COLUMNS.order_status - 1] = order.source_status
        if refused:
            row[COLUMNS.reporting_state - 1] = REPORTING_EXCLUDED_REFUSAL
        rows.append(row)
    return rows
