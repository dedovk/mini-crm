from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from crm_sync.clients.http import ApiError, HttpClient
from crm_sync.models import Order, OrderItem
from crm_sync.utils import (
    classify_payment,
    decimal_value,
    extract_ttn,
    first_value,
    nested_value,
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
        offset = 0
        limit = 100
        headers = {"Authorization": f"Bearer {self.token}"}
        while True:
            payload = self.http.request_json(
                "GET",
                f"{self.base_url}/orders/list",
                headers=headers,
                params={"limit": limit, "offset": offset},
            )
            if not isinstance(payload, dict):
                raise ApiError("Prom orders response must be an object")
            page = payload.get("orders") or payload.get("data") or []
            if not isinstance(page, list):
                raise ApiError("Prom orders response does not contain an orders list")
            raw_orders.extend(order for order in page if isinstance(order, dict))
            if len(page) < limit:
                break
            offset += limit

        normalized: list[Order] = []
        for raw in raw_orders:
            try:
                order = self._normalize(raw)
            except (ValueError, TypeError) as exc:
                LOGGER.warning("Prom order skipped because normalization failed: %s", exc)
                continue
            if order.created_at >= since - timedelta(days=1) and order.tracking_number and order.items:
                normalized.append(order)
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
            items.append(
                OrderItem(
                    name=str(first_value(product, "name", "title", "product_name")),
                    sku=str(first_value(product, "sku", "external_id", "article", "id")),
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
        delivery = raw.get("delivery_data") if isinstance(raw.get("delivery_data"), dict) else {}
        payment = raw.get("payment_option")
        payment_text = (
            str(first_value(payment, "name", "title")) if isinstance(payment, dict) else str(payment or "")
        )
        name = " ".join(
            str(first_value(raw, key)).strip()
            for key in ("client_last_name", "client_first_name", "client_second_name")
            if first_value(raw, key)
        )
        ttn = extract_ttn(
            first_value(raw, "ttn", "declaration_number", "delivery_declaration_id"),
            first_value(delivery, "ttn", "declaration_number", "declaration_id"),
            note,
        )
        return Order(
            source=self.source,
            external_id=str(first_value(raw, "id", "order_id")),
            created_at=parse_datetime(first_value(raw, "date_created", "created_at", "created"), self.timezone),
            customer_name=name or str(first_value(raw, "client_name", "customer_name")),
            city=str(
                nested_value(
                    raw,
                    (("delivery_data", "city"), ("delivery_data", "city_name"), ("delivery_address", "city")),
                    default=first_value(raw, "delivery_city", "city"),
                )
            ),
            phone=normalize_phone(first_value(raw, "phone", "client_phone", "customer_phone")),
            tracking_number=ttn,
            total=decimal_value(first_value(raw, "full_price", "total_price", "price", "total")),
            payment_method=classify_payment(payment_text, note),
            note=note,
            sender=str(first_value(delivery, "sender", "sender_name")),
            items=items,
        )

