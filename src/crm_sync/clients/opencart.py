from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urljoin

from crm_sync.clients.http import ApiError, HttpClient
from crm_sync.models import Order, OrderItem
from crm_sync.utils import (
    classify_payment,
    decimal_value,
    find_tracking_number,
    first_value,
    normalize_phone,
    parse_datetime,
    parse_optional_datetime,
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
        raw_count = 0
        without_tracking = 0
        without_items = 0
        not_completed = 0
        rejected_statuses: Counter[str] = Counter()
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
            raw_count += len(raw_orders)
            for raw in raw_orders:
                if not isinstance(raw, dict):
                    continue
                if not self._is_completed(raw):
                    not_completed += 1
                    status = str(
                        first_value(
                            raw,
                            "order_status",
                            "status_name",
                            "status",
                            "order_status_id",
                            default="missing",
                        )
                    ).strip()
                    rejected_statuses[status or "missing"] += 1
                    continue
                try:
                    order = self._normalize(raw)
                except (ValueError, TypeError) as exc:
                    LOGGER.warning("OpenCart order skipped because normalization failed: %s", exc)
                    continue
                if not order.tracking_number:
                    without_tracking += 1
                    continue
                if not order.items:
                    without_items += 1
                    continue
                orders.append(order)
            if len(raw_orders) < limit:
                break
            offset += limit
        LOGGER.info(
            "OpenCart fetched %s raw order(s): %s not completed, %s without tracking number, %s without products",
            raw_count,
            not_completed,
            without_tracking,
            without_items,
        )
        if rejected_statuses:
            LOGGER.warning("OpenCart rejected completion statuses: %s", dict(rejected_statuses))
        return orders

    @staticmethod
    def _is_completed(raw: dict[str, Any]) -> bool:
        completion_flag = str(raw.get("is_completed", "")).strip().casefold()
        if raw.get("is_completed") is True or completion_flag in {"1", "true", "yes"}:
            return True
        status = str(first_value(raw, "order_status", "status_name", "status")).strip().casefold()
        return any(marker in status for marker in ("виконан", "выполн", "заверш", "complete"))

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
                    product_code=str(first_value(product, "product_id", "id", "model", "sku")),
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
        created_at = parse_datetime(first_value(raw, "date_added", "created_at"), self.timezone)
        exact_completed_at = parse_optional_datetime(first_value(raw, "completed_at"), self.timezone)
        completed_at = exact_completed_at or parse_optional_datetime(
            first_value(raw, "date_modified", "changed", "updated_at"), self.timezone
        ) or created_at
        return Order(
            source=self.source,
            external_id=str(first_value(raw, "order_id", "id")),
            created_at=created_at,
            completed_at=completed_at,
            customer_name=full_name,
            city=str(first_value(raw, "shipping_city", "payment_city")),
            phone=normalize_phone(first_value(raw, "telephone", "phone")),
            tracking_number=find_tracking_number(
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
            completion_is_exact=exact_completed_at is not None,
            items=items,
        )
