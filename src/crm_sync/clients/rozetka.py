from __future__ import annotations

import base64
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from crm_sync.clients.http import ApiError, HttpClient
from crm_sync.models import Order, OrderExpenseTransaction, OrderItem
from crm_sync.utils import (
    classify_payment,
    collect_note_text,
    decimal_value,
    display_text,
    find_tracking_number,
    first_value,
    nested_value,
    normalize_phone,
    parse_datetime,
    parse_optional_datetime,
    parse_prepayment,
    person_name,
)

LOGGER = logging.getLogger(__name__)
# Rozetka Seller API order status identifiers:
# 3 - transferred to delivery service, 4 - delivering, 5 - at pickup point,
# 6 - received. Status 26 means "processing" and must not be treated as shipped.
ROZETKA_SHIPPED_STATUS_IDS = frozenset({3, 4, 5})
ROZETKA_COMPLETED_STATUS_IDS = frozenset({6})


def _payment_text(raw: dict[str, Any]) -> str:
    """Collect payment labels from both list and expanded Rozetka payloads."""
    direct = first_value(
        raw,
        "payment_type_name",
        "payment_method_name",
        "payment_name",
        "payment_type",
        "payment_method",
        "payment_status",
    )
    candidates: list[str] = []
    if direct not in (None, ""):
        candidates.append(display_text(direct) if isinstance(direct, dict) else str(direct))

    def visit(value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized_key = str(key).casefold()
                next_path = (*path, normalized_key)
                if isinstance(nested, (dict, list)):
                    visit(nested, next_path)
                elif any(
                    marker in normalized_key or any(marker in part for part in next_path)
                    for marker in ("payment", "pay", "credit", "installment", "част", "розстр")
                ):
                    text = str(nested or "").strip()
                    if text and not text.isdigit():
                        candidates.append(text)
        elif isinstance(value, list):
            for nested in value:
                visit(nested, path)

    visit(raw)
    unique = list(dict.fromkeys(candidate.strip() for candidate in candidates if candidate.strip()))
    installment = next(
        (
            candidate
            for candidate in unique
            if any(term in candidate.casefold() for term in ("част", "розстр", "installment", "credit"))
        ),
        "",
    )
    return installment or " | ".join(unique)


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
        try:
            payload = self.http.request_json(
                "GET",
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
        except ApiError as exc:
            if exc.status_code not in {401, 403} or not (self.username and self.password):
                raise
            self._active_token = self._login_token()
            payload = self.http.request_json(
                "GET",
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self._active_token}"},
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
        observed_at = datetime.now(ZoneInfo(self.timezone))
        orders_by_key: dict[str, Order] = {}
        details_by_order_id: dict[str, dict[str, Any] | None] = {}
        # Rozetka groups active and completed orders separately. Query both groups
        # and then keep only the exact business statuses supported by the CRM.
        for order_type in (2, 3):
            page_number = 1
            while True:
                payload = self._request_authorized(
                    "/orders/search",
                    params={
                        "page": page_number,
                        "changed_from": since.strftime("%Y-%m-%d"),
                        "types": order_type,
                        "sort": "-changed",
                        "expand": (
                            "user,delivery,purchases,status_data,payment_type_name,"
                            "payment_status,status_payment,credit_info,order_status_history"
                        ),
                    },
                )
                content = payload.get("content")
                if not isinstance(content, dict):
                    raise ApiError("Rozetka order search content must be an object")
                raw_orders = content.get("orders")
                if not isinstance(raw_orders, list):
                    raise ApiError("Rozetka order search orders must be a list")
                for raw in raw_orders:
                    if not isinstance(raw, dict):
                        continue
                    source_status = self._eligible_source_status(raw)
                    search_has_status = self._has_status_fields(raw)
                    if not source_status and search_has_status:
                        continue
                    normalized_raw = raw
                    # Search rows can omit comments, TTNs, or item lines. Hydrate only
                    # incomplete rows and cache the result across duplicate groups/pages.
                    if (
                        not source_status
                        or self._search_order_needs_details(raw)
                        or (
                            source_status == "Виконано"
                            and not self._has_shipped_history(raw)
                        )
                    ):
                        order_id = str(first_value(raw, "id", "order_id")).strip()
                        if order_id:
                            if order_id not in details_by_order_id:
                                try:
                                    detail_payload = self._request_authorized(
                                        f"/orders/{order_id}",
                                        params={
                                            "expand": (
                                                "user,delivery,purchases,status_data,payment_type_name,"
                                                "payment_status,status_payment,credit_info,item_details,"
                                                "order_status_history"
                                            )
                                        },
                                    )
                                except ApiError as exc:
                                    LOGGER.warning(
                                        "Rozetka order %s details are unavailable; using search data: %s",
                                        order_id,
                                        exc,
                                    )
                                    details_by_order_id[order_id] = None
                                else:
                                    detail = detail_payload.get("content")
                                    details_by_order_id[order_id] = (
                                        detail if isinstance(detail, dict) else None
                                    )
                            detail = details_by_order_id[order_id]
                            if detail is not None:
                                if self._has_status_fields(detail):
                                    source_status = self._eligible_source_status(detail)
                                    if not source_status:
                                        LOGGER.info(
                                            "Rozetka order %s became ineligible while details were loaded",
                                            order_id,
                                        )
                                        continue
                                normalized_raw = self._merge_search_and_detail(raw, detail)
                    if not source_status:
                        LOGGER.warning(
                            "Rozetka order %s was skipped because its status is unavailable",
                            str(first_value(raw, "id", "order_id")).strip(),
                        )
                        continue
                    try:
                        order = self._normalize(
                            normalized_raw,
                            source_status=source_status,
                            observed_at=observed_at,
                        )
                    except (ValueError, TypeError) as exc:
                        LOGGER.warning("Rozetka order skipped because normalization failed: %s", exc)
                        continue
                    if not order.tracking_number or not order.items:
                        LOGGER.warning(
                            "Rozetka order %s with status %s was skipped because %s",
                            order.external_id,
                            source_status,
                            "tracking number is missing"
                            if not order.tracking_number
                            else "items are missing",
                        )
                        continue
                    key = order.sync_key.casefold()
                    existing = orders_by_key.get(key)
                    orders_by_key[key] = (
                        self._merge_order_versions(existing, order) if existing else order
                    )
                meta = content.get("_meta") or {}
                if not isinstance(meta, dict):
                    raise ApiError("Rozetka order search metadata must be an object")
                try:
                    page_count = int(meta.get("pageCount") or page_number)
                except (TypeError, ValueError) as exc:
                    raise ApiError("Rozetka order search pageCount must be an integer") from exc
                if page_count < page_number or page_count > 10_000:
                    raise ApiError(f"Rozetka order search returned invalid pageCount={page_count}")
                if page_number >= page_count:
                    break
                page_number += 1
        return list(orders_by_key.values())

    @staticmethod
    def _status_name(raw: dict[str, Any]) -> str:
        status_data = raw.get("status_data") if isinstance(raw.get("status_data"), dict) else {}
        return display_text(
            first_value(
                status_data,
                "name",
                "title",
                "status_name",
                "status_title",
                default=first_value(raw, "status_name", "status_title"),
            )
        )

    @staticmethod
    def _search_order_needs_details(raw: dict[str, Any]) -> bool:
        """Return whether search data lacks fields required for a complete CRM row."""
        purchases = raw.get("purchases")
        delivery = raw.get("delivery") if isinstance(raw.get("delivery"), dict) else {}
        notes = collect_note_text(raw)
        tracking = find_tracking_number(
            first_value(raw, "ttn", "tracking_number", "declaration_number"),
            first_value(
                delivery,
                "ttn",
                "tracking_number",
                "declaration_number",
                "document_number",
            ),
            *notes,
        )
        return (
            not notes
            or not isinstance(purchases, list)
            or not any(isinstance(item, dict) for item in purchases)
            or not tracking
        )

    @staticmethod
    def _has_status_fields(raw: dict[str, Any]) -> bool:
        """Return whether a response contains an authoritative current-order status."""
        direct = (
            raw.get(key)
            for key in ("status", "status_id", "status_name", "status_title")
        )
        if any(value is not None and str(value).strip() for value in direct):
            return True
        status_data = raw.get("status_data")
        if isinstance(status_data, dict) and any(
            status_data.get(key) is not None and str(status_data.get(key)).strip()
            for key in ("id", "status_id", "name", "title", "status_name", "status_title")
        ):
            return True
        status_group = first_value(
            raw,
            "status_group",
            default=(status_data or {}).get("status_group")
            if isinstance(status_data, dict)
            else "",
        )
        try:
            return int(status_group) == 2
        except (TypeError, ValueError):
            return False

    @classmethod
    def _has_shipped_history(cls, raw: dict[str, Any]) -> bool:
        """Return whether status history contains the shipment accounting milestone."""
        history = raw.get("order_status_history")
        if not isinstance(history, list):
            return False
        for entry in history:
            if not isinstance(entry, dict):
                continue
            nested_status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
            status_id = first_value(
                entry,
                "status_id",
                default=first_value(nested_status, "id", "status_id"),
            )
            status_name = first_value(
                entry,
                "status_name",
                "status_title",
                default=first_value(nested_status, "name", "title"),
            )
            if cls._eligible_source_status(
                {"status": status_id, "status_name": status_name}
            ) == "Відправлено":
                return True
        return False

    @staticmethod
    def _merge_search_and_detail(
        search: dict[str, Any], detail: dict[str, Any]
    ) -> dict[str, Any]:
        """Merge eventually-consistent details without erasing complete search values."""
        merged = dict(search)
        nested_fields = {"delivery", "user", "status_data"}
        for key, value in detail.items():
            if value is None or value == "" or value == [] or value == {}:
                continue
            if key in nested_fields and isinstance(value, dict):
                existing = merged.get(key)
                merged[key] = {
                    **(existing if isinstance(existing, dict) else {}),
                    **{
                        nested_key: nested_value
                        for nested_key, nested_value in value.items()
                        if nested_value is not None and nested_value != ""
                    },
                }
                continue
            merged[key] = value
        return merged

    @staticmethod
    def _merge_order_versions(existing: Order, candidate: Order) -> Order:
        """Keep the newest lifecycle state while preserving the first accounting milestone."""
        rank = {"відправлено": 1, "виконано": 2, "скасовано": 3}
        preferred = (
            candidate
            if rank.get(candidate.source_status.casefold(), 0)
            >= rank.get(existing.source_status.casefold(), 0)
            else existing
        )
        versions = (
            (existing.completed_at, existing.completion_is_exact),
            (candidate.completed_at, candidate.completion_is_exact),
        )
        exact_versions = tuple(value for value in versions if value[1])
        earliest = min(exact_versions or versions, key=lambda value: value[0])
        preferred.completed_at = earliest[0]
        preferred.completion_is_exact = earliest[1]
        return preferred

    @classmethod
    def _eligible_source_status(cls, raw: dict[str, Any]) -> str:
        status_data = raw.get("status_data") if isinstance(raw.get("status_data"), dict) else {}
        status_name = cls._status_name(raw).casefold()
        if status_name.startswith(
            ("скасовано", "отменен", "відмова", "отказ", "canceled", "cancelled")
        ):
            return "Скасовано"
        status_group = first_value(raw, "status_group", default=status_data.get("status_group"))
        try:
            if int(status_group) == 2:
                return "Виконано"
        except (TypeError, ValueError):
            pass
        raw_status = first_value(
            raw,
            "status",
            "status_id",
            default=first_value(status_data, "id", "status_id"),
        )
        try:
            status_id = int(raw_status)
            if status_id in ROZETKA_COMPLETED_STATUS_IDS:
                return "Виконано"
            if status_id in ROZETKA_SHIPPED_STATUS_IDS:
                return "Відправлено"
            # A known numeric status is authoritative. In particular, do not
            # let an inconsistent localized label turn status 26 (processing)
            # into an eligible shipment.
            return ""
        except (TypeError, ValueError):
            pass
        if status_name.startswith(("виконано", "выполнен")):
            return "Виконано"
        if status_name.startswith(("відправлено", "отправлен")):
            return "Відправлено"
        return ""

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

    def _normalize(
        self,
        raw: dict[str, Any],
        *,
        source_status: str = "Виконано",
        observed_at: datetime | None = None,
    ) -> Order:
        purchases = raw.get("purchases") or []
        items: list[OrderItem] = []
        for purchase in purchases:
            if not isinstance(purchase, dict):
                continue
            item = purchase.get("item") if isinstance(purchase.get("item"), dict) else {}
            quantity = decimal_value(first_value(purchase, "quantity", default=1), Decimal(1))
            unit_price = decimal_value(first_value(purchase, "price_with_discount", "price"))
            line_total = decimal_value(first_value(purchase, "cost_with_discount", "cost"), quantity * unit_price)
            if quantity > 0 and line_total > 0 and unit_price <= 1 and line_total / quantity > 1:
                unit_price = line_total / quantity
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

        note = " | ".join(collect_note_text(raw))
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
        payment_text = _payment_text(raw)
        created_at = parse_datetime(first_value(raw, "created", "created_at"), self.timezone)
        current_status = first_value(
            raw,
            "status",
            "status_id",
            default=nested_value(raw, (("status_data", "id"),)),
        )
        history = raw.get("order_status_history") or []
        shipped_transitions: list[datetime] = []
        current_transitions: list[datetime] = []
        if isinstance(history, list):
            for entry in history:
                if not isinstance(entry, dict):
                    continue
                nested_status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
                status_id = first_value(
                    entry,
                    "status_id",
                    default=first_value(nested_status, "id", "status_id"),
                )
                status_name = first_value(
                    entry,
                    "status_name",
                    "status_title",
                    default=first_value(nested_status, "name", "title"),
                )
                transition_at = parse_optional_datetime(
                    first_value(entry, "created", "created_at"), self.timezone
                )
                if transition_at is None:
                    continue
                history_status = self._eligible_source_status(
                    {"status": status_id, "status_name": status_name}
                )
                if history_status == "Відправлено":
                    shipped_transitions.append(transition_at)
                if str(status_id) == str(current_status):
                    current_transitions.append(transition_at)
        history_transition = (
            min(shipped_transitions)
            if shipped_transitions
            else max(current_transitions, default=None)
        )
        explicit_transition = first_value(
            raw, "status_changed_at", "order_status_modified"
        )
        completion_value = history_transition or explicit_transition
        if source_status == "Виконано" and not completion_value:
            completion_value = first_value(raw, "changed", "updated_at")
        completed_at = parse_optional_datetime(
            completion_value,
            self.timezone,
        ) or observed_at or datetime.now(ZoneInfo(self.timezone))
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
            payment_method=classify_payment(payment_text, note) or "наложка",
            note=note,
            sender=str(nested_value(raw, (("delivery", "sender", "name"),), default="")),
            completion_is_exact=bool(completion_value),
            source_status=source_status,
            items=items,
            prepayment=parse_prepayment(note),
        )
