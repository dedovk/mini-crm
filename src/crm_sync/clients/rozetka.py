from __future__ import annotations

import base64
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from crm_sync.clients.http import ApiError, HttpClient
from crm_sync.models import Order, OrderExpenseTransaction, OrderItem
from crm_sync.utils import (
    classify_payment,
    decimal_value,
    display_text,
    find_tracking_number,
    first_value,
    nested_value,
    normalize_phone,
    parse_datetime,
    parse_optional_datetime,
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
        self._active_token = token

    def _login_token(self) -> str:
        if not self.username or not self.password:
            return ""
        payload = self.http.request_json(
            "POST",
            f"{self.base_url}/sites",
            json={
                "username": self.username,
                "password": base64.b64encode(self.password.encode("utf-8")).decode("ascii"),
            },
        )
        if not isinstance(payload, dict) or not payload.get("success"):
            errors = payload.get("errors") if isinstance(payload, dict) else None
            if isinstance(errors, dict):
                code = errors.get("code", "unknown")
                message = errors.get("message", "unknown")
                raise ApiError(f"Rozetka login failed: code={code}, message={message}")
            raise ApiError("Rozetka login failed: malformed API response")
        content = payload.get("content") or {}
        token = content.get("access_token") or content.get("token") if isinstance(content, dict) else ""
        if not token:
            raise ApiError("Rozetka login response does not contain a token")
        return str(token)

    def _authorization_token(self) -> str:
        if self._active_token:
            return self._active_token
        self._active_token = self._login_token()
        return self._active_token

    def _request_authorized(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        token = self._authorization_token()
        if not token:
            raise ApiError("Rozetka authorization is not configured")
        payload = self.http.request_json(
            "GET",
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        if isinstance(payload, dict) and payload.get("success"):
            return payload
        if self.username and self.password:
            self._active_token = self._login_token()
            payload = self.http.request_json(
                "GET",
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self._active_token}"},
                params=params,
            )
        if not isinstance(payload, dict) or not payload.get("success"):
            errors = payload.get("errors") if isinstance(payload, dict) else payload
            raise ApiError(f"Rozetka request failed for {path}: {errors}")
        return payload

    def fetch_orders(self, since: datetime) -> list[Order]:
        token = self._authorization_token()
        if not token:
            LOGGER.info("Rozetka sync skipped: token or login credentials are not configured")
            return []
        page_number = 1
        orders: list[Order] = []
        while True:
            payload = self._request_authorized(
                "/orders/search",
                params={
                    "page": page_number,
                    "changed_from": since.strftime("%Y-%m-%d"),
                    "types": 3,
                    "sort": "-changed",
                    "expand": "user,delivery,purchases,status_data",
                },
            )
            content = payload.get("content") or {}
            raw_orders = content.get("orders") or [] if isinstance(content, dict) else []
            for raw in raw_orders:
                if not isinstance(raw, dict):
                    continue
                status_data = raw.get("status_data") if isinstance(raw.get("status_data"), dict) else {}
                status_group = first_value(raw, "status_group", default=status_data.get("status_group"))
                try:
                    is_completed = int(status_group) == 2
                except (TypeError, ValueError):
                    is_completed = False
                if not is_completed:
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

    @staticmethod
    def _operation_entries(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
        content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
        raw = content.get(key) if isinstance(content, dict) else None
        if isinstance(raw, dict):
            return [entry for entry in raw.values() if isinstance(entry, dict)]
        if isinstance(raw, list):
            return [entry for entry in raw if isinstance(entry, dict)]
        return []

    @staticmethod
    def _operation_id(
        entries: list[dict[str, Any]],
        *,
        names: tuple[str, ...] = (),
        title_terms: tuple[str, ...] = (),
        default: int,
    ) -> int:
        wanted_names = {name.casefold() for name in names}
        for entry in entries:
            name = str(entry.get("name") or "").strip().casefold()
            title = str(entry.get("title") or "").strip().casefold()
            if name in wanted_names or (title_terms and all(term.casefold() in title for term in title_terms)):
                try:
                    return int(entry.get("id"))
                except (TypeError, ValueError):
                    continue
        return default

    def _fetch_pages(
        self,
        path: str,
        *,
        params: dict[str, Any],
        collection_key: str,
    ) -> list[dict[str, Any]]:
        page = 1
        transactions: list[dict[str, Any]] = []
        while True:
            page_params = {**params, "page": page}
            payload = self._request_authorized(path, params=page_params)
            content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
            raw_transactions = content.get(collection_key) if isinstance(content, dict) else []
            if isinstance(raw_transactions, list):
                transactions.extend(entry for entry in raw_transactions if isinstance(entry, dict))
            meta = content.get("_meta") if isinstance(content, dict) else {}
            try:
                page_count = int(meta.get("pageCount") or page) if isinstance(meta, dict) else page
            except (TypeError, ValueError):
                page_count = page
            if page >= page_count:
                break
            page += 1
        return transactions

    def fetch_expenses(self, since: datetime) -> dict[str, Decimal]:
        if not self._authorization_token():
            LOGGER.info("Rozetka expense sync skipped: authorization is not configured")
            return {}

        search_data = self._request_authorized("/balances/search-data", params={})
        royalty_types = self._operation_entries(search_data, "operationTypes")
        logistic_types = self._operation_entries(search_data, "operationTypesLogistic")
        try:
            logistic_search_data = self._request_authorized(
                "/balance-logistic/search-data", params={}
            )
        except ApiError as exc:
            # Some seller roles can read logistics transactions but cannot read this
            # optional filter dictionary. The stable operation IDs from the general
            # balance search metadata remain available as a fallback.
            LOGGER.warning("Rozetka logistics filter metadata is unavailable: %s", exc)
        else:
            logistic_types.extend(self._operation_entries(logistic_search_data, "operationTypes"))

        royalty_type = self._operation_id(
            royalty_types,
            names=("sale_commiss",),
            title_terms=("комісі", "продаж"),
            default=2,
        )
        delivery_type = self._operation_id(
            logistic_types,
            names=("withdrawallogisticmp",),
            title_terms=("достав", "відправ"),
            default=34,
        )
        refund_type_ids = {
            35,
            *(
                int(entry["id"])
                for entry in logistic_types
                if str(entry.get("id") or "").isdigit()
                and (
                    str(entry.get("name") or "").casefold() == "adjustmentlogisticmpup"
                    or (
                        "коригув" in str(entry.get("title") or "").casefold()
                        and "+" in str(entry.get("title") or "")
                    )
                    or "повернення списання" in str(entry.get("title") or "").casefold()
                    or "відшкодуван" in str(entry.get("title") or "").casefold()
                )
            ),
        }

        royalty_raw = self._fetch_pages(
            "/balances/search",
            params={
                "operationType": royalty_type,
                "dateFrom": since.strftime("%Y-%m-%d"),
                "pageSize": 100,
                "sort": "-logId",
            },
            collection_key="billingLogUserBalances",
        )
        using_logistics_fallback = False
        try:
            logistics_raw = self._fetch_pages(
                "/balance-logistic/search",
                params={"created_from": since.strftime("%Y-%m-%d"), "sort": "-operation_id"},
                collection_key="logisticBalances",
            )
        except ApiError as exc:
            # Older/static seller tokens may expose logistics through the legacy
            # balance endpoint while denying the dedicated logistics module.
            LOGGER.warning("Rozetka dedicated logistics history is unavailable; using balance history: %s", exc)
            using_logistics_fallback = True
            logistics_raw = []
            for operation_type in sorted({delivery_type, *refund_type_ids}):
                logistics_raw.extend(
                    self._fetch_pages(
                        "/balances/search",
                        params={
                            "operationType": operation_type,
                            "dateFrom": since.strftime("%Y-%m-%d"),
                            "pageSize": 100,
                            "sort": "-logId",
                        },
                        collection_key="billingLogUserBalances",
                    )
                )

        transactions: list[OrderExpenseTransaction] = []
        for raw in royalty_raw:
            if str(first_value(raw, "operationType", "operation_type")) != str(royalty_type):
                continue
            order_id = str(first_value(raw, "orderId", "order_id")).strip()
            transaction_id = str(first_value(raw, "id", "logId", "operation_id")).strip()
            if order_id and transaction_id:
                transactions.append(
                    OrderExpenseTransaction(
                        transaction_id=transaction_id,
                        order_id=order_id,
                        category="royalty",
                        debit=decimal_value(raw.get("debit")),
                        credit=decimal_value(raw.get("credit")),
                    )
                )

        for raw in logistics_raw:
            order_id = str(first_value(raw, "order_id", "orderId")).strip()
            transaction_id = str(first_value(raw, "operation_id", "id", "logId")).strip()
            if not order_id or not transaction_id:
                continue
            try:
                operation_type = int(first_value(raw, "operation_type", "operationType"))
            except (TypeError, ValueError):
                continue
            debit = decimal_value(raw.get("debit"))
            credit = decimal_value(raw.get("credit"))
            title = str(first_value(raw, "operation_type_title", "operationTypeTitle")).casefold()
            if operation_type == delivery_type:
                category = "logistics_charge"
            elif (
                operation_type in refund_type_ids and (credit != 0 or debit != 0)
            ) or (credit != 0 and ("повернен" in title or "відшкодуван" in title)):
                category = "logistics_refund"
            else:
                continue
            transactions.append(
                OrderExpenseTransaction(
                    transaction_id=transaction_id,
                    order_id=order_id,
                    category=category,
                    debit=debit,
                    credit=credit,
                )
            )

        category_counts = {
            category: sum(transaction.category == category for transaction in transactions)
            for category in ("royalty", "logistics_charge", "logistics_refund")
        }
        LOGGER.info(
            "Rozetka finance normalized %s royalty, %s logistics charge and %s logistics refund transaction(s)",
            category_counts["royalty"],
            category_counts["logistics_charge"],
            category_counts["logistics_refund"],
        )
        if (
            using_logistics_fallback
            and category_counts["royalty"] > 0
            and category_counts["logistics_charge"] == 0
        ):
            raise ApiError(
                "Rozetka token does not expose logistics transactions; configure "
                "ROZETKA_USERNAME and ROZETKA_PASSWORD for a full JWT session"
            )

        royalty_totals: dict[str, Decimal] = {}
        logistics_totals: dict[str, Decimal] = {}
        seen: set[tuple[str, str]] = set()
        for transaction in transactions:
            stream = "royalty" if transaction.category == "royalty" else "logistics"
            key = (stream, transaction.transaction_id)
            if key in seen:
                continue
            seen.add(key)
            target = royalty_totals if stream == "royalty" else logistics_totals
            target[transaction.order_id] = (
                target.get(transaction.order_id, Decimal(0)) + transaction.expense_effect
            )
        order_ids = royalty_totals.keys() | logistics_totals.keys()
        return {
            order_id: max(Decimal(0), royalty_totals.get(order_id, Decimal(0)))
            + max(Decimal(0), logistics_totals.get(order_id, Decimal(0)))
            for order_id in order_ids
        }

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
        created_at = parse_datetime(first_value(raw, "created", "created_at"), self.timezone)
        current_status = first_value(raw, "status", default=nested_value(raw, (("status_data", "id"),)))
        history = raw.get("order_status_history") or []
        history_timestamp: Any = ""
        if isinstance(history, list):
            for entry in reversed(history):
                if not isinstance(entry, dict):
                    continue
                status_id = first_value(entry, "status_id", default=nested_value(entry, (("status", "id"),)))
                if str(status_id) == str(current_status):
                    history_timestamp = first_value(entry, "created", "created_at")
                    break
        completion_value = history_timestamp or first_value(
            raw, "changed", "status_updated_at", "updated_at"
        )
        completed_at = parse_optional_datetime(
            completion_value,
            self.timezone,
        ) or created_at
        return Order(
            source=self.source,
            external_id=str(first_value(raw, "id", "order_id")),
            created_at=created_at,
            completed_at=completed_at,
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
            completion_is_exact=bool(completion_value),
            items=items,
        )
