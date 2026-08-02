from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from crm_sync.clients.http import ApiError, HttpClient
from crm_sync.models import Order, OrderItem
from crm_sync.utils import (
    city_from_address,
    classify_payment,
    decimal_value,
    display_text,
    find_tracking_number,
    first_value,
    normalize_phone,
    parse_datetime,
)

LOGGER = logging.getLogger(__name__)


class PromClient:
    source = "prom"

    def __init__(self, http: HttpClient, *, token: str, base_url: str, timezone: str) -> None:
        self.http = http
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timezone = timezone

    def fetch_orders(self, since: datetime) -> list[Order]:
        if not self.token:
            LOGGER.info("Prom sync skipped: PROM_API_TOKEN is not configured")
            return []
        raw_orders: list[dict[str, Any]] = []
        limit = 100
        last_id: int | None = None
        seen_cursors: set[int] = set()
        headers = {"Authorization": f"Bearer {self.token}"}
        date_from = (since - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
        date_to = datetime.now(since.tzinfo).strftime("%Y-%m-%dT%H:%M:%S")
        while True:
            params: dict[str, Any] = {
                "limit": limit,
                "date_from": date_from,
                "date_to": date_to,
            }
            if last_id is not None:
                params["last_id"] = last_id
            payload = self.http.request_json(
                "GET",
                f"{self.base_url}/orders/list",
                headers=headers,
                params=params,
            )
            if not isinstance(payload, dict):
                raise ApiError("Prom orders response must be an object")
            page = payload.get("orders") or payload.get("data") or []
            if not isinstance(page, list):
                raise ApiError("Prom orders response does not contain an orders list")
            raw_orders.extend(order for order in page if isinstance(order, dict))
            if len(page) < limit:
                break
            try:
                next_last_id = min(int(order["id"]) for order in page if isinstance(order, dict)) - 1
            except (KeyError, TypeError, ValueError) as exc:
                raise ApiError("Prom pagination requires numeric order IDs") from exc
            if next_last_id in seen_cursors or (last_id is not None and next_last_id >= last_id):
                raise ApiError("Prom pagination cursor did not advance")
            seen_cursors.add(next_last_id)
            last_id = next_last_id

        normalized: list[Order] = []
        without_tracking = 0
        without_items = 0
        diagnostic_order_fields: set[str] = set()
        diagnostic_product_fields: set[str] = set()
        diagnostic_values: set[str] = set()

        def describe(value: Any) -> str:
            if isinstance(value, dict):
                return f"dict({','.join(sorted(value))})"
            if isinstance(value, list):
                return f"list[{len(value)}]"
            return repr(value)[:80]

        for raw in raw_orders:
            diagnostic_order_fields.update(
                key
                for key in raw
                if any(marker in key.casefold() for marker in ("price", "cost", "sum", "sale", "catalog", "commission", "cpa"))
            )
            for product in raw.get("products") or []:
                if not isinstance(product, dict):
                    continue
                interesting = {
                    key
                    for key in product
                    if any(
                        marker in key.casefold()
                        for marker in ("price", "cost", "sum", "sale", "catalog", "commission", "cpa", "sku", "external", "id")
                    )
                }
                diagnostic_product_fields.update(interesting)
                diagnostic_values.update(f"product.{key}={describe(product.get(key))}" for key in interesting)
            try:
                order = self._normalize(raw)
            except (ValueError, TypeError) as exc:
                LOGGER.warning("Prom order skipped because normalization failed: %s", exc)
                continue
            if order.created_at < since - timedelta(days=1):
                continue
            if not order.tracking_number:
                without_tracking += 1
                continue
            if not order.items:
                without_items += 1
                continue
            normalized.append(order)
        LOGGER.info(
            "Prom fetched %s raw order(s): %s without tracking number, %s without products",
            len(raw_orders),
            without_tracking,
            without_items,
        )
        LOGGER.info(
            "Prom commercial diagnostics: order_fields=%s product_fields=%s values=%s",
            sorted(diagnostic_order_fields),
            sorted(diagnostic_product_fields),
            sorted(diagnostic_values)[:40],
        )
        return normalized

    def _normalize(self, raw: dict[str, Any]) -> Order:
        products = raw.get("products") or raw.get("items") or []
        items: list[OrderItem] = []
        for product in products:
            if not isinstance(product, dict):
                continue
            quantity = decimal_value(first_value(product, "quantity", "count", default=1), Decimal(1))
            unit_price = decimal_value(first_value(product, "price", "unit_price"))
            line_total = decimal_value(first_value(product, "total_price", "total", "sum"), quantity * unit_price)
            product_url = str(first_value(product, "url"))
            url_code = re.search(r"/p(\d+)(?:-|\.html|$)", product_url)
            items.append(
                OrderItem(
                    name=str(first_value(product, "name", "title", "product_name")),
                    product_code=(
                        url_code.group(1)
                        if url_code
                        else str(first_value(product, "product_id", "id", "external_id", "sku", "article"))
                    ),
                    quantity=quantity,
                    unit_price=unit_price,
                    line_total=line_total,
                )
            )

        note = " | ".join(
            str(value).strip()
            for value in (
                first_value(raw, "client_notes", "client_note"),
                first_value(raw, "seller_notes", "seller_note", "notes", "comment"),
            )
            if value
        )
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
        return Order(
            source=self.source,
            external_id=str(first_value(raw, "id", "order_id")),
            created_at=parse_datetime(first_value(raw, "date_created", "created_at", "created"), self.timezone),
            customer_name=recipient_name or client_name or str(first_value(raw, "client_name", "customer_name")),
            city=display_text(first_value(recipient, "city_name", "city", "locality"))
            or city_from_address(first_value(delivery, "recipient_address"))
            or city_from_address(first_value(raw, "delivery_address")),
            phone=normalize_phone(first_value(raw, "phone", "client_phone", "customer_phone")),
            tracking_number=ttn,
            total=decimal_value(first_value(raw, "full_price", "total_price", "price", "total")),
            payment_method=classify_payment(payment_text, note),
            note=note,
            sender=str(first_value(delivery, "sender", "sender_name")),
            items=items,
        )
