from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from crm_sync.clients.http import ApiError, HttpClient
from crm_sync.models import InstallmentCommissionSource, Order, OrderItem
from crm_sync.utils import (
    city_from_address,
    classify_payment,
    collect_note_text,
    decimal_value,
    display_text,
    find_tracking_number,
    first_value,
    normalize_phone,
    parse_datetime,
    parse_optional_datetime,
    parse_prepayment,
)

LOGGER = logging.getLogger(__name__)

INSTALLMENT_RATES = {
    2: Decimal("0.034"), 3: Decimal("0.037"), 4: Decimal("0.048"),
    5: Decimal("0.065"), 6: Decimal("0.077"), 7: Decimal("0.089"),
    8: Decimal("0.100"), 9: Decimal("0.113"), 10: Decimal("0.125"),
    12: Decimal("0.150"), 15: Decimal("0.183"), 18: Decimal("0.215"),
    24: Decimal("0.276"),
}
COMMISSION_LABEL_MARKERS = ("комис", "коміс", "commission", "fee")


def _is_installment_commission_label(value: Any) -> bool:
    normalized = re.sub(r"[_-]+", " ", display_text(value).casefold())
    is_installment = (
        ("част" in normalized and "оплат" in normalized)
        or "installment" in normalized
        or ("pay" in normalized and "part" in normalized)
    )
    return is_installment and any(marker in normalized for marker in COMMISSION_LABEL_MARKERS)


def _find_named_value(value: Any, names: set[str]) -> Any:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).casefold() in names and nested not in (None, ""):
                return nested
        for nested in value.values():
            found = _find_named_value(nested, names)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_named_value(nested, names)
            if found not in (None, ""):
                return found
    return None


def _find_installment_commission_by_label(value: Any) -> Any:
    """Find Prom's named 'pay by installments' commission in nested payloads."""
    if isinstance(value, dict):
        for key, nested in value.items():
            if _is_installment_commission_label(key):
                candidate = (
                    first_value(nested, "amount", "value", "sum", "price", "cost")
                    if isinstance(nested, dict)
                    else nested
                )
                amount = abs(decimal_value(candidate))
                if amount > 0:
                    return amount
        label = " ".join(
            display_text(value.get(key))
            for key in ("name", "title", "type", "description", "label")
            if value.get(key) not in (None, "")
        ).casefold()
        if _is_installment_commission_label(label):
            amount = first_value(value, "amount", "value", "sum", "price", "cost")
            normalized_amount = abs(decimal_value(amount))
            if normalized_amount > 0:
                return normalized_amount
        for nested in value.values():
            found = _find_installment_commission_by_label(nested)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_installment_commission_by_label(nested)
            if found not in (None, ""):
                return found
    return None


def _installment_cost(
    raw: dict[str, Any],
    payment_text: str,
    total: Decimal,
    *,
    fallback_rate: Decimal,
) -> tuple[Decimal, InstallmentCommissionSource]:
    """Return fee and provenance used to obtain it.

    The lookup order is an explicit API field, a labeled nested charge, a
    supported payment-count tariff, and finally the configured fallback rate.
    All results are positive and rounded to kopecks.
    """
    explicit = _find_named_value(
        raw,
        {
            "installment_commission", "installments_commission", "payment_parts_commission",
            "parts_payment_commission", "pay_parts_commission", "credit_commission",
            "installment_fee", "payment_parts_fee", "pay_parts_fee",
        },
    )
    explicit_amount = abs(decimal_value(explicit))
    if explicit_amount > 0:
        return explicit_amount, "reported"
    labeled_amount = decimal_value(_find_installment_commission_by_label(raw))
    if labeled_amount > 0:
        return labeled_amount, "reported"

    count_value = _find_named_value(
        raw,
        {
            "installments_count", "installment_count", "parts_count", "payments_count",
            "payment_parts_count", "pay_parts_count", "credit_parts_count",
        },
    )
    count_match = re.search(r"(?<!\d)(\d{1,2})(?!\d)", payment_text)
    try:
        count = int(decimal_value(count_value)) if count_value not in (None, "") else 0
    except (TypeError, ValueError):
        count = 0
    if not count and count_match:
        count = int(count_match.group(1))
    rate = INSTALLMENT_RATES.get(count)
    if rate and total > 0:
        return (
            (total * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            "tariff",
        )
    if count == 0 and fallback_rate > 0 and total > 0:
        return (
            (total * fallback_rate).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            ),
            "fallback",
        )
    return Decimal(0), ""


class PromClient:
    source = "prom"

    def __init__(
        self,
        http: HttpClient,
        *,
        token: str,
        base_url: str,
        timezone: str,
        installment_fallback_rate: Decimal = Decimal(0),
    ) -> None:
        self.http = http
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timezone = timezone
        self.installment_fallback_rate = installment_fallback_rate

    def fetch_orders(self, since: datetime) -> list[Order]:
        if not self.token:
            LOGGER.info("Prom sync skipped: PROM_API_TOKEN is not configured")
            return []
        headers = {"Authorization": f"Bearer {self.token}"}
        observed_at = datetime.now(since.tzinfo)
        date_from = (since - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        date_to = observed_at.strftime("%Y-%m-%dT%H:%M:%S")
        raw_by_id: dict[str, dict[str, Any]] = {}
        for status in ("delivered", "canceled"):
            try:
                pages = self._fetch_status_pages(
                    status=status,
                    modified_from=date_from,
                    modified_to=date_to,
                    headers=headers,
                )
            except ApiError:
                if status == "delivered":
                    raise
                LOGGER.info("Prom does not accept status spelling %s; continuing", status)
                continue
            for raw in pages:
                order_id = str(first_value(raw, "id", "order_id"))
                current = raw_by_id.get(order_id)
                if current is None or str(raw.get("status", "")).casefold() in {"canceled", "cancelled"}:
                    raw_by_id[order_id] = raw
        raw_orders = list(raw_by_id.values())

        normalized: list[Order] = []
        not_completed = 0
        without_tracking = 0
        without_items = 0
        for raw in raw_orders:
            raw_status = str(raw.get("status", "")).strip().casefold()
            if raw_status not in {"delivered", "canceled", "cancelled"}:
                not_completed += 1
                continue
            try:
                order = self._normalize(raw, observed_at=observed_at)
            except (ValueError, TypeError) as exc:
                LOGGER.warning("Prom order skipped because normalization failed: %s", exc)
                continue
            if order.is_cancelled:
                normalized.append(order)
                continue
            if not order.tracking_number:
                without_tracking += 1
                continue
            if not order.items:
                without_items += 1
                continue
            normalized.append(order)
        LOGGER.info(
            "Prom fetched %s raw order(s): %s not completed, %s without tracking number, %s without products",
            len(raw_orders),
            not_completed,
            without_tracking,
            without_items,
        )
        normalized_items = [item for order in normalized for item in order.items]
        LOGGER.info(
            "Prom normalized commercial data: %s/%s items with price, %s/%s with SKU, %s/%s orders with ProSale cost",
            sum(item.unit_price > 0 for item in normalized_items),
            len(normalized_items),
            sum(bool(item.product_code.strip()) for item in normalized_items),
            len(normalized_items),
            sum(order.advertising_cost > 0 for order in normalized),
            len(normalized),
        )
        return normalized

    def _fetch_status_pages(
        self,
        *,
        status: str,
        modified_from: str,
        modified_to: str,
        headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        limit = 100
        last_id: int | None = None
        seen_cursors: set[int] = set()
        while True:
            params: dict[str, Any] = {
                "limit": limit,
                "last_modified_from": modified_from,
                "last_modified_to": modified_to,
                "status": status,
            }
            if last_id is not None:
                params["last_id"] = last_id
            payload = self.http.request_json(
                "GET", f"{self.base_url}/orders/list", headers=headers, params=params
            )
            if not isinstance(payload, dict):
                raise ApiError("Prom orders response must be an object")
            page = payload.get("orders") or payload.get("data") or []
            if not isinstance(page, list):
                raise ApiError("Prom orders response does not contain an orders list")
            result.extend(order for order in page if isinstance(order, dict))
            if len(page) < limit:
                return result
            try:
                next_last_id = min(int(order["id"]) for order in page if isinstance(order, dict)) - 1
            except (KeyError, TypeError, ValueError) as exc:
                raise ApiError("Prom pagination requires numeric order IDs") from exc
            if next_last_id in seen_cursors or (last_id is not None and next_last_id >= last_id):
                raise ApiError("Prom pagination cursor did not advance")
            seen_cursors.add(next_last_id)
            last_id = next_last_id

    def _normalize(self, raw: dict[str, Any], *, observed_at: datetime | None = None) -> Order:
        products = raw.get("products") or raw.get("items") or []
        items: list[OrderItem] = []
        for product in products:
            if not isinstance(product, dict):
                continue
            quantity = decimal_value(first_value(product, "quantity", "count", default=1), Decimal(1))
            unit_price = decimal_value(
                first_value(product, "price_with_discount", "price", "unit_price", "final_price")
            )
            line_total = decimal_value(
                first_value(product, "total_price", "total", "sum", "line_total"),
                quantity * unit_price,
            )
            if quantity > 0 and line_total > 0 and unit_price <= 1 and line_total / quantity > 1:
                unit_price = line_total / quantity
            items.append(
                OrderItem(
                    name=str(first_value(product, "name", "title", "product_name")),
                    product_code=str(first_value(product, "sku", "article", "external_id", "product_id", "id")),
                    quantity=quantity,
                    unit_price=unit_price,
                    line_total=line_total,
                )
            )

        note_parts = collect_note_text(raw)
        note = " | ".join(dict.fromkeys(note_parts))
        delivery = next(
            (
                raw.get(key)
                for key in ("delivery_provider_data", "delivery_data")
                if isinstance(raw.get(key), dict)
            ),
            {},
        )
        recipient = raw.get("delivery_recipient") if isinstance(raw.get("delivery_recipient"), dict) else {}
        payment = raw.get("payment_option")
        payment_text = (
            str(first_value(payment, "name", "title")) if isinstance(payment, dict) else str(payment or "")
        )
        prosale = raw.get("prosale_commission")
        cpa_commission = raw.get("cpa_commission")
        advertising_cost = decimal_value(prosale) or decimal_value(cpa_commission)
        total = decimal_value(first_value(raw, "full_price", "total_price", "price", "total"))
        payment_method = classify_payment(payment_text, note)
        installment_commission = Decimal(0)
        installment_source: InstallmentCommissionSource = ""
        if payment_method == "оплата частями":
            installment_commission, installment_source = _installment_cost(
                raw,
                payment_text,
                total,
                fallback_rate=self.installment_fallback_rate,
            )
            if installment_source == "fallback":
                LOGGER.warning(
                    "Prom installment commission for order %s was estimated at "
                    "configured rate %s because the API omitted fee details",
                    first_value(raw, "id", "order_id"),
                    self.installment_fallback_rate,
                )
            elif installment_commission == 0:
                LOGGER.warning(
                    "Prom installment order %s has no explicit commission or "
                    "supported payment count",
                    first_value(raw, "id", "order_id"),
                )
        recipient_name = " ".join(
            str(first_value(recipient, key)).strip()
            for key in ("last_name", "first_name", "second_name")
            if first_value(recipient, key)
        )
        client_name = " ".join(
            str(first_value(raw, key)).strip()
            for key in ("client_last_name", "client_first_name", "client_second_name")
            if first_value(raw, key)
        )
        ttn = find_tracking_number(
            first_value(
                delivery,
                "ttn",
                "declaration_number",
                "tracking_number",
                "document_number",
                "waybill_number",
            ),
            first_value(raw, "ttn", "declaration_number", "tracking_number", "document_number"),
            note,
        )
        created_at = parse_datetime(first_value(raw, "date_created", "created_at", "created"), self.timezone)
        exact_completed_at = parse_optional_datetime(
            first_value(raw, "completed_at", "status_changed_at", "order_status_modified"),
            self.timezone,
        )
        completed_at = exact_completed_at or observed_at or created_at
        return Order(
            source=self.source,
            external_id=str(first_value(raw, "id", "order_id")),
            created_at=created_at,
            completed_at=completed_at,
            customer_name=recipient_name or client_name or str(first_value(raw, "client_name", "customer_name")),
            city=display_text(first_value(recipient, "city_name", "city", "locality"))
            or city_from_address(first_value(delivery, "recipient_address"))
            or city_from_address(first_value(raw, "delivery_address")),
            phone=normalize_phone(first_value(raw, "phone", "client_phone", "customer_phone")),
            tracking_number=ttn,
            total=total,
            payment_method=payment_method,
            note=note,
            sender=str(first_value(delivery, "sender", "sender_name")),
            completion_is_exact=exact_completed_at is not None,
            source_status=(
                "Скасовано"
                if str(raw.get("status", "")).strip().casefold() in {"canceled", "cancelled"}
                else "Виконано"
            ),
            items=items,
            prepayment=parse_prepayment(note),
            advertising_cost=advertising_cost,
            installment_commission=installment_commission,
            installment_commission_source=installment_source,
        )
