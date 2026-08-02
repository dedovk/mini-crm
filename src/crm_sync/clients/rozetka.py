from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from crm_sync.clients.http import ApiError, HttpClient
from crm_sync.models import Order, OrderItem
from crm_sync.utils import (
    classify_payment,
    decimal_value,
    display_text,
    find_tracking_number,
    first_value,
    nested_value,
    normalize_phone,
    parse_datetime,
    person_name,
)

LOGGER = logging.getLogger(__name__)


class RozetkaClient:
    source = "rozetka"

    def __init__(
        self,
        http: HttpClient,
        *,
        token: str,
        username: str,
        password: str,
        base_url: str,
        timezone: str,
    ) -> None:
        self.http = http
        self.token = token
        self.username = username
        self.password = password
        self.base_url = base_url.rstrip("/")
        self.timezone = timezone

    def _login_token(self) -> str:
        if not self.username or not self.password:
            return ""
        payload = self.http.request_json(
            "POST",
            f"{self.base_url}/site/login",
            json={"username": self.username, "password": self.password},
        )
        if not isinstance(payload, dict) or not payload.get("success"):
            raise ApiError("Rozetka login failed")
        content = payload.get("content") or {}
        token = content.get("access_token") or content.get("token") if isinstance(content, dict) else ""
        if not token:
            raise ApiError("Rozetka login response does not contain a token")
        return str(token)

    def _authorization_token(self) -> str:
        if self.token:
            return self.token
        return self._login_token()

    def fetch_orders(self, since: datetime) -> list[Order]:
        token = self._authorization_token()
        if not token:
            LOGGER.info("Rozetka sync skipped: token or login credentials are not configured")
            return []
        headers = {"Authorization": f"Bearer {token}"}
        page_number = 1
        orders: list[Order] = []
        while True:
            payload = self.http.request_json(
                "GET",
                f"{self.base_url}/orders/search",
                headers=headers,
                params={
                    "page": page_number,
                    "changed_from": since.strftime("%Y-%m-%d"),
                    "types": 1,
                    "sort": "-changed",
                    "expand": "user,delivery,purchases,status_data",
                },
            )
            if (
                isinstance(payload, dict)
                and not payload.get("success")
                and token == self.token
                and self.username
                and self.password
            ):
                token = self._login_token()
                headers = {"Authorization": f"Bearer {token}"}
                payload = self.http.request_json(
                    "GET",
                    f"{self.base_url}/orders/search",
                    headers=headers,
                    params={
                        "page": page_number,
                        "changed_from": since.strftime("%Y-%m-%d"),
                        "types": 1,
                        "sort": "-changed",
                        "expand": "user,delivery,purchases,status_data",
                    },
                )
            if not isinstance(payload, dict) or not payload.get("success"):
                raise ApiError(f"Rozetka orders search failed: {payload.get('errors') if isinstance(payload, dict) else payload}")
            content = payload.get("content") or {}
            raw_orders = content.get("orders") or [] if isinstance(content, dict) else []
            for raw in raw_orders:
                if not isinstance(raw, dict):
                    continue
                try:
                    order = self._normalize(raw)
                except (ValueError, TypeError) as exc:
                    LOGGER.warning("Rozetka order skipped because normalization failed: %s", exc)
                    continue
                if order.tracking_number and order.items:
                    orders.append(order)
            meta = content.get("_meta") or {} if isinstance(content, dict) else {}
            page_count = int(meta.get("pageCount") or page_number)
            if page_number >= page_count:
                break
            page_number += 1
        return orders

    def _normalize(self, raw: dict[str, Any]) -> Order:
        purchases = raw.get("purchases") or []
        items: list[OrderItem] = []
        for purchase in purchases:
            if not isinstance(purchase, dict):
                continue
            item = purchase.get("item") if isinstance(purchase.get("item"), dict) else {}
            quantity = decimal_value(first_value(purchase, "quantity", default=1), Decimal(1))
            unit_price = decimal_value(first_value(purchase, "price_with_discount", "price"))
            line_total = decimal_value(first_value(purchase, "cost_with_discount", "cost"), quantity * unit_price)
            items.append(
                OrderItem(
                    name=str(first_value(purchase, "item_name", default=first_value(item, "name", "name_ua"))),
                    product_code=str(
                        first_value(
                            purchase,
                            "item_id",
                            "product_id",
                            default=first_value(item, "id", "item_id", "article", "price_offer_id"),
                        )
                    ),
                    quantity=quantity,
                    unit_price=unit_price,
                    line_total=line_total,
                )
            )

        seller_comments = raw.get("seller_comment") or []
        comment_parts = [str(first_value(raw, "current_seller_comment", "comment")).strip()]
        if isinstance(seller_comments, list):
            comment_parts.extend(
                str(entry.get("comment", "")).strip()
                for entry in seller_comments
                if isinstance(entry, dict)
            )
        note = " | ".join(dict.fromkeys(part for part in comment_parts if part))
        user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        delivery = raw.get("delivery") if isinstance(raw.get("delivery"), dict) else {}
        locality = delivery.get("locality") if isinstance(delivery.get("locality"), dict) else {}
        recipient = delivery.get("recipient") if isinstance(delivery.get("recipient"), dict) else {}
        recipient_name = person_name(
            first_value(
                raw,
                "user_title",
                "recipient_title",
                "recipient_name",
                default=first_value(
                    delivery,
                    "recipient_title",
                    "recipient_name",
                    "contact_person",
                    default=first_value(recipient, "name", "full_name", "title"),
                ),
            )
        )
        user_name = person_name(first_value(user, "name", "full_name", "title")) or person_name(user)
        payment_text = str(first_value(raw, "payment_type_name", "payment_type", "payment_status"))
        return Order(
            source=self.source,
            external_id=str(first_value(raw, "id", "order_id")),
            created_at=parse_datetime(first_value(raw, "created", "created_at"), self.timezone),
            customer_name=recipient_name or user_name,
            city=display_text(first_value(locality, "title", "name_ua", "name", default=first_value(delivery, "city"))),
            phone=normalize_phone(first_value(raw, "user_phone", default=first_value(user, "phone"))),
            tracking_number=find_tracking_number(
                first_value(raw, "ttn", "tracking_number", "declaration_number"),
                first_value(delivery, "ttn", "tracking_number", "declaration_number", "document_number"),
                note,
            ),
            total=decimal_value(first_value(raw, "cost_with_discount", "cost", "amount_with_discount", "amount")),
            # The orders/search response does not expose payment fields for this seller.
            # The Rozetka cabinet labels these orders as payment on receipt.
            payment_method=classify_payment(payment_text, note) or "наложка",
            note=note,
            sender=str(nested_value(raw, (("delivery", "sender", "name"),), default="")),
            items=items,
        )
