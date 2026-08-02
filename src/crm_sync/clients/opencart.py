from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urljoin

from crm_sync.clients.http import ApiError, HttpClient
from crm_sync.models import Order, OrderItem
from crm_sync.utils import (
    classify_payment,
    decimal_value,
    extract_ttn,
    first_value,
    normalize_phone,
    parse_datetime,
)

LOGGER = logging.getLogger(__name__)


class OpenCartClient:
    source = "opencart"

    def __init__(
        self,
        http: HttpClient,
        *,
        base_url: str,
        api_key: str,
        endpoint: str,
        timezone: str,
    ) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/") + "/" if base_url else ""
        self.api_key = api_key
        self.endpoint = endpoint
        self.timezone = timezone

    def fetch_orders(self, since: datetime) -> list[Order]:
        if not self.base_url or not self.api_key or not self.endpoint:
            LOGGER.info("OpenCart sync skipped: endpoint or API key is not configured")
            return []
        orders: list[Order] = []
        offset = 0
        limit = 500
        while True:
            payload = self.http.request_json(
                "GET",
                urljoin(self.base_url, self.endpoint.lstrip("/")),
                headers={"X-CRM-API-Key": self.api_key},
                params={
                    "changed_from": since.strftime("%Y-%m-%d %H:%M:%S"),
                    "limit": limit,
                    "offset": offset,
                },
            )
            if not isinstance(payload, dict) or not payload.get("success"):
                raise ApiError(f"OpenCart CRM endpoint failed: {payload}")
            raw_orders = payload.get("orders") or []
            for raw in raw_orders:
                if not isinstance(raw, dict):
                    continue
                try:
                    order = self._normalize(raw)
                except (ValueError, TypeError) as exc:
                    LOGGER.warning("OpenCart order skipped because normalization failed: %s", exc)
                    continue
                if order.tracking_number and order.items:
                    orders.append(order)
            if len(raw_orders) < limit:
                break
            offset += limit
        return orders

    def _normalize(self, raw: dict[str, Any]) -> Order:
        products = raw.get("products") or []
        items: list[OrderItem] = []
        for product in products:
            if not isinstance(product, dict):
                continue
            quantity = decimal_value(first_value(product, "quantity", default=1), Decimal(1))
            unit_price = decimal_value(first_value(product, "price"))
            line_total = decimal_value(first_value(product, "total"), quantity * unit_price)
            items.append(
                OrderItem(
                    name=str(first_value(product, "name")),
                    sku=str(first_value(product, "model", "sku", "product_id")),
                    quantity=quantity,
                    unit_price=unit_price,
                    line_total=line_total,
                )
            )
        histories = raw.get("history_comments") or []
        note = " | ".join(
            dict.fromkeys(
                part.strip()
                for part in [str(first_value(raw, "comment", "notes")), *(str(v) for v in histories)]
                if part.strip()
            )
        )
        payment_text = str(first_value(raw, "payment_method", "payment_code"))
        full_name = " ".join(
            part for part in (str(raw.get("lastname", "")), str(raw.get("firstname", ""))) if part
        )
        return Order(
            source=self.source,
            external_id=str(first_value(raw, "order_id", "id")),
            created_at=parse_datetime(first_value(raw, "date_added", "created_at"), self.timezone),
            customer_name=full_name,
            city=str(first_value(raw, "shipping_city", "payment_city")),
            phone=normalize_phone(first_value(raw, "telephone", "phone")),
            tracking_number=extract_ttn(
                first_value(
                    raw,
                    "novaposhta_cn_number",
                    "ttn",
                    "tracking_number",
                    "declaration_number",
                ),
                note,
            ),
            total=decimal_value(first_value(raw, "total")),
            payment_method=classify_payment(payment_text, note),
            note=note,
            sender=str(first_value(raw, "sender", "store_name")),
            items=items,
        )
