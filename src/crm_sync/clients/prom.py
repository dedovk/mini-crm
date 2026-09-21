from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import AbstractSet, Any

from crm_sync.clients.http import ApiError, HttpClient
from crm_sync.models import InstallmentCommissionSource, Order, OrderItem
from crm_sync.sheet_schema import PROM_PAYMENT_METHOD
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

COMMISSION_LABEL_MARKERS = ("комис", "коміс", "commission", "fee")
PROM_SETTLED_PAYMENT_STATUSES = frozenset({"paid", "paid_out", "refunded"})
_DETAIL_FAILURE_LIMIT = 2


@dataclass(frozen=True, slots=True)
class InstallmentDetailDiagnostics:
    """Observable outcome of optional Prom installment-detail enrichment."""

    requested: int = 0
    resolved: int = 0
    not_reported: int = 0
    failures: int = 0
    skipped_by_circuit: int = 0

    @property
    def degraded(self) -> bool:
        return self.failures > 0 or self.skipped_by_circuit > 0


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
) -> tuple[Decimal, InstallmentCommissionSource]:
    """Return fee and provenance used to obtain it.

    Only an amount explicitly reported by Prom is accepted as financial data.
    Missing values stay unresolved instead of being estimated from a tariff.
    """
    explicit = _find_named_value(
        raw,
        {
            "installment_commission",
            "installments_commission",
            "payment_parts_commission",
            "parts_payment_commission",
            "pay_parts_commission",
            "credit_commission",
            "installment_fee",
            "payment_parts_fee",
            "pay_parts_fee",
        },
    )
    explicit_amount = abs(decimal_value(explicit))
    if explicit_amount > 0:
        return explicit_amount, "reported"
    labeled_amount = decimal_value(_find_installment_commission_by_label(raw))
    if labeled_amount > 0:
        return labeled_amount, "reported"
    return Decimal(0), "unresolved"


def _prom_payment_method(raw: dict[str, Any], note: str) -> str:
    """Classify Prom payments using both checkout choice and transaction state."""
    payment_option = raw.get("payment_option")
    payment_text = (
        str(first_value(payment_option, "name", "title"))
        if isinstance(payment_option, dict)
        else str(payment_option or "")
    )
    classified = classify_payment(payment_text, note)
    if classified in {"смешанная", "оплата частями"}:
        return classified

    payment_data = raw.get("payment_data")
    if isinstance(payment_data, dict):
        payment_type = str(first_value(payment_data, "type", "payment_type")).strip().casefold()
        payment_status = (
            str(first_value(payment_data, "status", "payment_status")).strip().casefold()
        )
        if (
            classified == "наложка"
            and payment_type == "evopay"
            and payment_status in PROM_SETTLED_PAYMENT_STATUSES
        ):
            return PROM_PAYMENT_METHOD
    return classified


class PromClient:
    source = "prom"

    def __init__(
        self,
        http: HttpClient,
        *,
        token: str,
        base_url: str,
        timezone: str,
    ) -> None:
        self.http = http
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timezone = timezone
        self._installment_detail_cache: dict[
            str, tuple[Decimal, InstallmentCommissionSource]
        ] = {}
        self._detail_requested = 0
        self._detail_resolved = 0
        self._detail_not_reported = 0
        self._detail_failures = 0
        self._detail_skipped_by_circuit = 0
        self._detail_circuit_open = False

    @property
    def installment_detail_diagnostics(self) -> InstallmentDetailDiagnostics:
        """Return counters used by health checks and maintenance summaries."""
        return InstallmentDetailDiagnostics(
            requested=self._detail_requested,
            resolved=self._detail_resolved,
            not_reported=self._detail_not_reported,
            failures=self._detail_failures,
            skipped_by_circuit=self._detail_skipped_by_circuit,
        )

    def fetch_orders(self, since: datetime) -> list[Order]:
        return self.fetch_orders_between(since, datetime.now(since.tzinfo))

    def fetch_orders_between(
        self,
        since: datetime,
        until: datetime,
        *,
        payment_only: bool = False,
        include_installment_details: bool = True,
        installment_order_ids: AbstractSet[str] | None = None,
    ) -> list[Order]:
        """Fetch completed orders changed inside an explicit bounded interval.

        Detail enrichment is explicit so maintenance jobs that only reconcile
        payment labels do not generate unrelated per-order API requests.  A
        caller may also restrict enrichment to the sheet rows that actually
        need an authoritative installment commission.
        """
        if not self.token:
            LOGGER.info("Prom sync skipped: PROM_API_TOKEN is not configured")
            return []
        headers = {"Authorization": f"Bearer {self.token}"}
        observed_at = until
        date_from = (since - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        date_to = until.strftime("%Y-%m-%dT%H:%M:%S")
        raw_by_id: dict[str, dict[str, Any]] = {}
        for status in ("delivered", "canceled"):
            try:
                pages = self._fetch_status_pages(
                    status=status,
                    modified_from=date_from,
                    modified_to=date_to,
                    headers=headers,
                )
            except ApiError as exc:
                if status == "delivered" or exc.status_code not in {400, 422}:
                    raise
                LOGGER.info("Prom does not accept status spelling %s; continuing", status)
                continue
            for raw in pages:
                order_id = str(first_value(raw, "id", "order_id"))
                current = raw_by_id.get(order_id)
                if current is None or str(raw.get("status", "")).casefold() in {
                    "canceled",
                    "cancelled",
                }:
                    raw_by_id[order_id] = raw
        raw_orders = list(raw_by_id.values())
        if include_installment_details:
            raw_orders = [
                self._with_installment_detail(
                    raw,
                    headers,
                    eligible_order_ids=installment_order_ids,
                )
                for raw in raw_orders
            ]

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
            if not payment_only and not order.tracking_number:
                without_tracking += 1
                continue
            if not payment_only and not order.items:
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

    def _with_installment_detail(
        self,
        raw: dict[str, Any],
        headers: dict[str, str],
        *,
        eligible_order_ids: AbstractSet[str] | None = None,
    ) -> dict[str, Any]:
        """Enrich an installment order with its official Prom detail payload."""
        note = " | ".join(dict.fromkeys(collect_note_text(raw)))
        if _prom_payment_method(raw, note) != "оплата частями":
            return raw
        if _installment_cost(raw)[1] == "reported":
            return raw
        order_id = str(first_value(raw, "id", "order_id")).strip()
        if not order_id:
            LOGGER.warning("Prom installment order has no ID; commission is unresolved")
            return raw
        if eligible_order_ids is not None and order_id not in eligible_order_ids:
            return raw
        if order_id in self._installment_detail_cache:
            return self._merge_installment_detail(
                raw,
                self._installment_detail_cache[order_id],
            )
        if self._detail_circuit_open:
            self._detail_skipped_by_circuit += 1
            LOGGER.warning(
                "Prom installment detail circuit is open; order %s remains unresolved",
                order_id,
            )
            return raw
        self._detail_requested += 1
        try:
            payload = self.http.request_json(
                "GET",
                f"{self.base_url}/orders/{order_id}",
                headers=headers,
                retry_limit=0,
            )
        except ApiError as exc:
            self._record_detail_failure()
            LOGGER.warning(
                "Prom installment detail for order %s is unavailable; commission is unresolved: %s",
                order_id,
                exc,
            )
            return raw
        detail = self._detail_order(payload)
        if detail is None:
            self._record_detail_failure()
            LOGGER.warning(
                "Prom installment detail for order %s has an unsupported shape; commission is unresolved",
                order_id,
            )
            return raw
        detail_id = str(first_value(detail, "id", "order_id")).strip()
        if detail_id and detail_id != order_id:
            self._record_detail_failure()
            LOGGER.warning(
                "Prom installment detail ID mismatch for order %s: received %s",
                order_id,
                detail_id,
            )
            return raw
        commission = _installment_cost(detail)
        self._installment_detail_cache[order_id] = commission
        if commission[1] == "reported":
            self._detail_resolved += 1
        else:
            self._detail_not_reported += 1
        return self._merge_installment_detail(raw, commission)

    def _record_detail_failure(self) -> None:
        """Open the detail circuit after repeated failures in one run."""
        self._detail_failures += 1
        if self._detail_failures >= _DETAIL_FAILURE_LIMIT:
            self._detail_circuit_open = True

    @staticmethod
    def _merge_installment_detail(
        raw: dict[str, Any],
        commission: tuple[Decimal, InstallmentCommissionSource],
    ) -> dict[str, Any]:
        """Attach only the needed financial value, never unrelated detail fields."""
        amount, source = commission
        if source != "reported" or amount <= 0:
            return raw
        merged = dict(raw)
        merged["installment_commission"] = amount
        return merged

    @staticmethod
    def _detail_order(payload: Any) -> dict[str, Any] | None:
        """Extract one order from supported Prom detail response envelopes."""
        if not isinstance(payload, dict):
            return None
        for key in ("order", "data"):
            candidate = payload.get(key)
            if isinstance(candidate, dict):
                nested = candidate.get("order")
                return nested if isinstance(nested, dict) else candidate
            if isinstance(candidate, list) and len(candidate) == 1:
                return candidate[0] if isinstance(candidate[0], dict) else None
        if any(key in payload for key in ("id", "order_id")):
            return payload
        return None

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
                next_last_id = (
                    min(int(order["id"]) for order in page if isinstance(order, dict)) - 1
                )
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
            quantity = decimal_value(
                first_value(product, "quantity", "count", default=1), Decimal(1)
            )
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
                    product_code=str(
                        first_value(product, "sku", "article", "external_id", "product_id", "id")
                    ),
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
        recipient = (
            raw.get("delivery_recipient") if isinstance(raw.get("delivery_recipient"), dict) else {}
        )
        prosale = raw.get("prosale_commission")
        cpa_commission = raw.get("cpa_commission")
        advertising_cost = decimal_value(prosale) or decimal_value(cpa_commission)
        total = decimal_value(first_value(raw, "full_price", "total_price", "price", "total"))
        payment_method = _prom_payment_method(raw, note)
        installment_commission = Decimal(0)
        installment_source: InstallmentCommissionSource = ""
        if payment_method == "оплата частями":
            installment_commission, installment_source = _installment_cost(raw)
            if installment_commission == 0:
                LOGGER.warning(
                    "Prom installment order %s has no explicit commission; "
                    "profit remains unresolved",
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
        created_at = parse_datetime(
            first_value(raw, "date_created", "created_at", "created"), self.timezone
        )
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
            customer_name=recipient_name
            or client_name
            or str(first_value(raw, "client_name", "customer_name")),
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
