from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from crm_sync.clients.http import ApiError, HttpClient
from crm_sync.clients.opencart import OpenCartClient
from crm_sync.clients.prom import PromClient
from crm_sync.clients.rozetka import RozetkaClient


def test_rozetka_refreshes_expired_bearer_token() -> None:
    class ExpiredTokenHttp:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def request_json(self, method: str, url: str, **kwargs):
            self.calls.append((method, url))
            if method == "POST":
                return {"success": True, "content": {"access_token": "fresh-token"}}
            authorization = kwargs["headers"]["Authorization"]
            if authorization == "Bearer stale-token":
                raise ApiError("expired", status_code=401)
            return {"success": True, "content": {"orders": [], "_meta": {"pageCount": 1}}}

    http = ExpiredTokenHttp()
    client = RozetkaClient(
        http,  # type: ignore[arg-type]
        token="stale-token",
        username="seller@example.test",
        password="secret",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )

    payload = client._request_authorized("/orders/search", params={})

    assert payload["success"] is True
    assert client._active_token == "fresh-token"
    assert [method for method, _ in http.calls] == ["GET", "POST", "GET"]


def test_opencart_rejects_non_list_orders_collection() -> None:
    class MalformedOpenCartHttp:
        def request_json(self, method: str, url: str, **kwargs):
            return {"success": True, "orders": {"unexpected": "object"}}

    client = OpenCartClient(
        MalformedOpenCartHttp(),  # type: ignore[arg-type]
        base_url="https://example.test",
        api_key="test",
        endpoint="/api/orders",
        timezone="Europe/Kyiv",
    )

    with pytest.raises(ApiError, match="must be a list"):
        client.fetch_orders(datetime(2026, 8, 1, tzinfo=ZoneInfo("Europe/Kyiv")))


def test_rozetka_rejects_non_list_orders_collection() -> None:
    class MalformedRozetkaHttp:
        def request_json(self, method: str, url: str, **kwargs):
            return {
                "success": True,
                "content": {"orders": {"unexpected": "object"}, "_meta": {"pageCount": 1}},
            }

    client = RozetkaClient(
        MalformedRozetkaHttp(),  # type: ignore[arg-type]
        token="test-token",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )

    with pytest.raises(ApiError, match="orders must be a list"):
        client.fetch_orders(datetime(2026, 8, 1, tzinfo=ZoneInfo("Europe/Kyiv")))


def test_rozetka_normalizer_creates_item_rows() -> None:
    client = RozetkaClient(
        HttpClient(max_retries=0),
        token="test",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )
    order = client._normalize(
        {
            "id": 248888186,
            "created": "2026-08-01 10:20:59",
            "changed": "2026-08-02 11:22:33",
            "cost_with_discount": "640.00",
            "user_phone": "0501234567",
            "ttn": "20451234567890",
            "payment_type_name": "Наложенный платеж",
            "current_seller_comment": "перед - 200",
            "user": {"name": "Тестовий Клієнт"},
            "delivery": {"locality": {"name_ua": "Київ"}},
            "purchases": [
                {
                    "item_id": 10,
                    "item_name": "Товар",
                    "quantity": 2,
                    "price_with_discount": "320.00",
                    "cost_with_discount": "640.00",
                    "item": {"article": "SKU-10"},
                }
            ],
        }
    )
    assert order.sync_key == "rozetka:248888186"
    assert order.phone == "+380501234567"
    assert order.payment_method == "смешанная"
    assert order.prepayment == Decimal(200)
    assert order.items[0].product_code == "10"
    assert order.completed_at.strftime("%d.%m.%Y %H:%M") == "02.08.2026 11:22"
    assert order.completion_is_exact is True


def test_rozetka_normalizer_extracts_city_name_from_delivery_object() -> None:
    client = RozetkaClient(
        HttpClient(max_retries=0),
        token="test",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )

    order = client._normalize(
        {
            "id": 1,
            "created": "2026-08-01 10:20:59",
            "changed": "2026-08-02 09:15:00",
            "cost": 100,
            "ttn": "RMP-483122083",
            "user_title": "Тестовий Отримувач",
            "delivery": {"city": {"id": 330, "name": "Київ"}},
            "purchases": [{"item_name": "Товар", "quantity": 1, "price": 100}],
        }
    )

    assert order.city == "Київ"
    assert order.customer_name == "Тестовий Отримувач"
    assert order.tracking_number == "RMP-483122083"


def test_rozetka_normalizer_prefers_first_delivery_status_timestamp() -> None:
    client = RozetkaClient(
        HttpClient(max_retries=0),
        token="test",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )

    order = client._normalize(
        {
            "id": 2,
            "status": 5,
            "created": "2026-08-01 10:00:00",
            "changed": "2026-08-03 15:00:00",
            "order_status_history": [
                {"status_id": 3, "created": "2026-08-01 11:00:00"},
                {"status_id": 5, "created": "2026-08-02 12:34:00"},
            ],
            "ttn": "20451234567890",
            "purchases": [{"item_name": "Товар", "quantity": 1, "price": 100}],
        }
    )

    assert order.completed_at.strftime("%d.%m.%Y %H:%M") == "01.08.2026 11:00"
    assert order.completion_is_exact is True


def test_rozetka_normalizer_formats_nested_user_name_object() -> None:
    client = RozetkaClient(
        HttpClient(max_retries=0),
        token="token",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )
    order = client._normalize(
        {
            "id": 42,
            "created": "2026-08-01T12:00:00+03:00",
            "changed": "2026-08-02T12:00:00+03:00",
            "user": {
                "name": {
                    "first_name": "Олександр",
                    "last_name": "Зенькович",
                    "second_name": "Михайлович",
                }
            },
            "delivery": {"city": {"name": "Самар"}, "ttn": "20451500957753"},
            "purchases": [{"item_id": 608037110, "item_name": "Товар", "price": 999, "quantity": 1}],
        }
    )

    assert order.customer_name == "Зенькович Олександр Михайлович"


def test_prom_normalizer_uses_sku_as_product_code() -> None:
    client = PromClient(
        HttpClient(max_retries=0),
        token="test",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )

    order = client._normalize(
        {
            "id": 1,
            "status": "delivered",
            "date_created": "2026-08-02 12:00:00",
            "completed_at": "2026-08-03 08:45:00",
            "delivery_provider_data": {
                "declaration_number": "ЕН 20 4515 0157 2223",
                "recipient_address": "м. Київ, Відділення №1",
            },
            "delivery_recipient": {
                "last_name": "Тестовий",
                "first_name": "Отримувач",
            },
            "full_price": 100,
            "prosale_commission": {"value": 69.3},
            "products": [
                {
                    "id": "5110513990",
                    "external_id": "5110513990",
                    "sku": "JX27",
                    "url": "https://example.com/p608037110-tovar.html",
                    "name": "Товар",
                    "quantity": 1,
                    "price": 100,
                }
            ],
        }
    )

    assert order.tracking_number == "20 4515 0157 2223"
    assert order.city == "Київ"
    assert order.customer_name == "Тестовий Отримувач"
    assert order.items[0].product_code == "JX27"
    assert order.items[0].unit_price == 100
    assert order.advertising_cost == Decimal("69.3")
    assert order.completed_at.strftime("%H:%M") == "08:45"


def test_prom_normalizer_reads_uppercase_prepayment_from_client_notes() -> None:
    client = PromClient(
        HttpClient(max_retries=0),
        token="test",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )

    order = client._normalize(
        {
            "id": 420579152,
            "status": "delivered",
            "date_created": "2026-08-10 11:42:00",
            "client_notes": "Предоплата 200 грн.",
            "delivery_provider_data": {"declaration_number": "20451508042941"},
            "full_price": 1200,
            "payment_option": {"name": "Наложенный платеж"},
            "products": [
                {
                    "sku": "MSN-W3",
                    "name": "Масажна подушка",
                    "quantity": 1,
                    "price": 1200,
                }
            ],
        }
    )

    assert order.note == "Предоплата 200 грн."
    assert order.payment_method == "смешанная"


def test_prom_normalizer_reads_installment_commission_separately() -> None:
    client = PromClient(
        HttpClient(max_retries=0),
        token="test",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )
    order = client._normalize(
        {
            "id": 417697593,
            "status": "delivered",
            "date_created": "2026-07-25 12:33:00",
            "full_price": 4449,
            "payment_option": {"name": "Оплата частинами", "parts_count": 3},
            "prosale_commission": {"value": 295.86},
            "payment_data": {"installment_commission": {"amount": 164.61}},
            "products": [{"name": "Товар", "sku": "KR65-GR-5P", "quantity": 1, "price": 4449}],
        }
    )

    assert order.payment_method == "оплата частями"
    assert order.advertising_cost == Decimal("295.86")
    assert order.installment_commission == Decimal("164.61")
    assert order.installment_commission_source == "reported"


def test_prom_normalizer_calculates_installment_commission_from_parts_count() -> None:
    client = PromClient(
        HttpClient(max_retries=0), token="test", base_url="https://example.test", timezone="Europe/Kyiv"
    )
    order = client._normalize(
        {
            "id": 2,
            "status": "delivered",
            "date_created": "2026-07-25 12:33:00",
            "full_price": 4449,
            "payment_option": {"name": "Оплата частинами", "parts_count": 3},
            "products": [{"name": "Товар", "quantity": 1, "price": 4449}],
        }
    )

    assert order.installment_commission == Decimal("164.61")
    assert order.installment_commission_source == "tariff"


def test_prom_normalizer_uses_configured_fallback_rate_when_api_omits_fee_details() -> None:
    client = PromClient(
        HttpClient(max_retries=0),
        token="test",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
        installment_fallback_rate=Decimal("0.037"),
    )
    order = client._normalize(
        {
            "id": 421660654,
            "status": "delivered",
            "date_created": "2026-08-15 18:13:00",
            "full_price": 1329,
            "payment_option": {"name": "Оплата частинами"},
            "prosale_commission": 90.11,
            "products": [{"name": "Товар", "quantity": 1, "price": 1329}],
        }
    )

    assert order.advertising_cost == Decimal("90.11")
    assert order.installment_commission == Decimal("49.17")
    assert order.installment_commission_source == "fallback"


def test_prom_normalizer_does_not_guess_unknown_installment_count() -> None:
    client = PromClient(
        HttpClient(max_retries=0),
        token="test",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
        installment_fallback_rate=Decimal("0.037"),
    )
    order = client._normalize(
        {
            "id": 421660658,
            "status": "delivered",
            "date_created": "2026-08-15 18:13:00",
            "full_price": 1329,
            "payment_option": {"name": "Оплата частинами 11"},
            "products": [{"name": "Товар", "quantity": 1, "price": 1329}],
        }
    )

    assert order.installment_commission == 0


def test_prom_normalizer_reads_installment_commission_from_named_charge() -> None:
    client = PromClient(
        HttpClient(max_retries=0), token="test", base_url="https://example.test", timezone="Europe/Kyiv"
    )
    order = client._normalize(
        {
            "id": 421660654,
            "status": "delivered",
            "date_created": "2026-08-15 18:13:00",
            "full_price": 1329,
            "payment_option": {"name": "Оплата частинами"},
            "commissions": [
                {"name": "Комиссия за заказ", "amount": 90.11},
                {"name": "Комиссия по Оплатить частями", "amount": 49.17},
            ],
            "prosale_commission": 90.11,
            "products": [{"name": "Товар", "quantity": 1, "price": 1329}],
        }
    )

    assert order.advertising_cost == Decimal("90.11")
    assert order.installment_commission == Decimal("49.17")


def test_prom_normalizer_reads_installment_commission_from_labeled_key() -> None:
    client = PromClient(
        HttpClient(max_retries=0), token="test", base_url="https://example.test", timezone="Europe/Kyiv"
    )
    order = client._normalize(
        {
            "id": 421660655,
            "status": "delivered",
            "date_created": "2026-08-15 18:13:00",
            "full_price": 1329,
            "payment_option": {"name": "Оплата частинами"},
            "fees": {
                "Комиссия по Оплатить частями": {"amount": 49.17}
            },
            "products": [{"name": "Товар", "quantity": 1, "price": 1329}],
        }
    )

    assert order.installment_commission == Decimal("49.17")


def test_prom_normalizer_accepts_localized_negative_installment_fee() -> None:
    client = PromClient(
        HttpClient(max_retries=0), token="test", base_url="https://example.test", timezone="Europe/Kyiv"
    )
    order = client._normalize(
        {
            "id": 421660656,
            "status": "delivered",
            "date_created": "2026-08-15 18:13:00",
            "full_price": 1329,
            "payment_option": {"name": "Оплата частинами"},
            "fees": {"installment_fee": -49.17},
            "products": [{"name": "Товар", "quantity": 1, "price": 1329}],
        }
    )

    assert order.installment_commission == Decimal("49.17")


def test_prom_normalizer_accepts_ukrainian_installment_commission_label() -> None:
    client = PromClient(
        HttpClient(max_retries=0), token="test", base_url="https://example.test", timezone="Europe/Kyiv"
    )
    order = client._normalize(
        {
            "id": 421660657,
            "status": "delivered",
            "date_created": "2026-08-15 18:13:00",
            "full_price": 1329,
            "payment_option": {"name": "Оплата частинами"},
            "fees": {"Комісія за оплату частинами": 49.17},
            "products": [{"name": "Товар", "quantity": 1, "price": 1329}],
        }
    )

    assert order.installment_commission == Decimal("49.17")


def test_prom_normalizer_recovers_unit_price_and_nested_prepayment_note() -> None:
    client = PromClient(
        HttpClient(max_retries=0), token="test", base_url="https://example.test", timezone="Europe/Kyiv"
    )
    order = client._normalize(
        {
            "id": 421221060,
            "status": "delivered",
            "date_created": "2026-08-13 12:18:00",
            "full_price": 4449,
            "payment_option": {"name": "Наложенный платеж"},
            "seller_data": {"manager_comment": "Предоплата 1000 грн."},
            "products": [
                {
                    "name": "Крісло",
                    "sku": "KR65-GR-5P",
                    "quantity": 1,
                    "price": 1,
                    "total_price": 4449,
                }
            ],
        }
    )

    assert order.items[0].unit_price == Decimal("4449")
    assert order.items[0].line_total == Decimal("4449")
    assert order.payment_method == "смешанная"
    assert order.prepayment == Decimal(1000)


def test_rozetka_normalizer_detects_nested_installment_payment() -> None:
    client = RozetkaClient(
        HttpClient(max_retries=0),
        token="test",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )
    order = client._normalize(
        {
            "id": 902504468,
            "created": "2026-08-07 12:41:00",
            "changed": "2026-08-08 12:00:00",
            "cost": 2549,
            "payment": {"method": {"title": "Оплатити частинами від Rozetka 6"}},
            "ttn": "20451505735803",
            "purchases": [{"item_name": "Товар", "quantity": 1, "price": 2549}],
        }
    )

    assert order.payment_method == "оплата частями"


def test_opencart_normalizer_reads_nova_poshta_custom_field() -> None:
    client = OpenCartClient(
        HttpClient(max_retries=0),
        base_url="https://example.test",
        api_key="test",
        endpoint="/index.php?route=api/crm_orders",
        timezone="Europe/Kyiv",
    )
    order = client._normalize(
        {
            "order_id": "501",
            "order_status": "Виконано",
            "date_added": "2026-08-01 12:00:00",
            "date_modified": "2026-08-05 09:30:00",
            "completed_at": "2026-08-03 14:25:00",
            "firstname": "Клієнт",
            "lastname": "Тестовий",
            "telephone": "0501234567",
            "shipping_city": "Київ",
            "novaposhta_cn_number": "20451234567890",
            "total": "1000.00",
            "payment_method": "Наложенный платеж",
            "comment": "перед - 200",
            "products": [
                {
                    "product_id": "10",
                    "name": "Товар",
                    "model": "SKU-10",
                    "quantity": "2",
                    "price": "500.00",
                    "total": "1000.00",
                }
            ],
        }
    )
    assert order.tracking_number == "20451234567890"
    assert order.sender == "наш"
    assert order.sync_key == "opencart:501"
    assert order.payment_method == "смешанная"
    assert order.items[0].product_code == "10"
    assert order.completed_at.strftime("%d.%m.%Y %H:%M") == "03.08.2026 14:25"
    assert order.updated_at and order.updated_at.strftime("%d.%m.%Y %H:%M") == "05.08.2026 09:30"


def test_opencart_completion_accepts_boolean_text_and_localized_statuses() -> None:
    assert OpenCartClient._is_completed({"is_completed": "true"}) is True
    assert OpenCartClient._is_completed({"order_status": "Виконано"}) is True
    assert OpenCartClient._is_completed({"order_status": "Очікує обробки"}) is False
    assert OpenCartClient._channel({"order_status": "Сделка завершена"}) == "site"
    assert (
        OpenCartClient._channel({"order_status": "Сделка завершена(Заказ по тел)"})
        == "phone"
    )


class RozetkaSearchStub:
    def __init__(self) -> None:
        self.params: list[dict] = []

    def request_json(self, method: str, url: str, **kwargs):
        self.params.append(kwargs["params"])
        return {"success": True, "content": {"orders": [], "_meta": {"pageCount": 1}}}


def test_rozetka_search_requests_shipped_and_completed_orders() -> None:
    http = RozetkaSearchStub()
    client = RozetkaClient(
        http,  # type: ignore[arg-type]
        token="test",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )

    assert client.fetch_orders(datetime(2026, 8, 1, tzinfo=ZoneInfo("Europe/Kyiv"))) == []
    assert {params["types"] for params in http.params} == {2, 3}


class RozetkaShippedStub:
    def request_json(self, method: str, url: str, **kwargs):
        orders = []
        if kwargs["params"]["types"] == 2:
            orders = [
                {
                    "id": "902000001",
                    "created": "2026-08-01 10:00:00",
                    "changed": "2026-08-10 09:15:00",
                    "status_changed_at": "2026-08-10 09:15:00",
                    "status": 3,
                    "status_group": 1,
                    "status_data": {"id": 3, "name": "Передано в службу доставки", "status_group": 1},
                    "current_seller_comment": "предо 400",
                    "cost": "1200",
                    "user": {"name": "Тестовий Покупець", "phone": "0501234567"},
                    "delivery": {
                        "ttn": "RMP-123456789",
                        "locality": {"name": "Київ"},
                    },
                    "purchases": [
                        {
                            "item_id": "608037110",
                            "item_name": "Товар",
                            "quantity": 1,
                            "price": 1200,
                            "cost": 1200,
                        }
                    ],
                }
            ]
        return {
            "success": True,
            "content": {"orders": orders, "_meta": {"pageCount": 1}},
        }


def test_rozetka_shipped_order_uses_status_change_date_and_comment() -> None:
    client = RozetkaClient(
        RozetkaShippedStub(),  # type: ignore[arg-type]
        token="test",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )

    orders = client.fetch_orders(datetime(2026, 8, 1, tzinfo=ZoneInfo("Europe/Kyiv")))

    assert len(orders) == 1
    assert orders[0].source_status == "Відправлено"
    assert orders[0].completed_at.strftime("%d.%m.%Y %H:%M") == "10.08.2026 09:15"
    assert orders[0].note == "предо 400"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"status": 3}, "Відправлено"),
        ({"status_data": {"id": "4"}}, "Відправлено"),
        ({"status": 5}, "Відправлено"),
        ({"status": 6}, "Виконано"),
        ({"status": 26}, ""),
        ({"status": 26, "status_data": {"name": "Відправлено"}}, ""),
    ],
)
def test_rozetka_recognizes_only_supported_numeric_shipped_status(
    raw: dict[str, object], expected: str
) -> None:
    assert RozetkaClient._eligible_source_status(raw) == expected


class RozetkaIncompleteSearchRowStub:
    def __init__(self) -> None:
        self.detail_requests = 0

    def request_json(self, method: str, url: str, **kwargs):
        if url.endswith("/orders/902000003"):
            self.detail_requests += 1
            return {
                "success": True,
                "content": {
                    "id": "902000003",
                    "status": 3,
                    "status_changed_at": "2026-08-31 09:15:00",
                    "status_data": {"id": 3, "name": "Передано в службу доставки"},
                    "delivery": {
                        "ttn": "RMP-787478919",
                        "locality": {"name": "Київ"},
                    },
                    "purchases": [
                        {
                            "item_id": "SKU-1",
                            "item_name": "Товар",
                            "quantity": 1,
                            "price": 999,
                            "cost": 999,
                        }
                    ],
                },
            }
        orders = []
        if kwargs["params"]["types"] == 2:
            orders = [
                {
                    "id": "902000003",
                    "created": "2026-08-30 10:00:00",
                    "changed": "2026-08-31 09:15:00",
                    "status": 3,
                    "current_seller_comment": "передзвонити покупцю",
                    "cost": 999,
                }
            ]
        return {
            "success": True,
            "content": {"orders": orders, "_meta": {"pageCount": 1}},
        }


def test_rozetka_loads_details_when_search_row_has_comment_but_no_ttn_or_items() -> None:
    http = RozetkaIncompleteSearchRowStub()
    client = RozetkaClient(
        http,  # type: ignore[arg-type]
        token="test",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )

    orders = client.fetch_orders(datetime(2026, 8, 1, tzinfo=ZoneInfo("Europe/Kyiv")))

    assert http.detail_requests == 1
    assert len(orders) == 1
    assert orders[0].tracking_number == "RMP-787478919"
    assert orders[0].source_status == "Відправлено"
    assert orders[0].completed_at.strftime("%d.%m.%Y %H:%M") == "31.08.2026 09:15"
    assert len(orders[0].items) == 1


def test_rozetka_shipped_generic_changed_time_is_not_treated_as_transition() -> None:
    observed_at = datetime(2026, 8, 31, 12, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
    client = RozetkaClient(
        RozetkaSearchStub(),  # type: ignore[arg-type]
        token="test",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )
    raw = {
        "id": "902000004",
        "created": "2026-08-20 10:00:00",
        "changed": "2026-08-30 18:00:00",
        "status": 3,
        "delivery": {"ttn": "RMP-787478920"},
        "purchases": [
            {"item_id": "SKU", "item_name": "Товар", "quantity": 1, "price": 999}
        ],
        "cost": 999,
    }

    order = client._normalize(
        raw, source_status="Відправлено", observed_at=observed_at
    )

    assert order.completed_at == observed_at
    assert not order.completion_is_exact


class RozetkaCancelledDetailStub(RozetkaIncompleteSearchRowStub):
    def request_json(self, method: str, url: str, **kwargs):
        payload = super().request_json(method, url, **kwargs)
        if url.endswith("/orders/902000003"):
            payload["content"]["status"] = 99
            payload["content"]["status_data"] = {"id": 99, "name": "Скасовано"}
        return payload


def test_rozetka_returns_order_cancelled_before_detail_hydration_for_reconciliation() -> None:
    client = RozetkaClient(
        RozetkaCancelledDetailStub(),  # type: ignore[arg-type]
        token="test",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )

    orders = client.fetch_orders(
        datetime(2026, 8, 1, tzinfo=ZoneInfo("Europe/Kyiv"))
    )

    assert len(orders) == 1
    assert orders[0].source_status == "Скасовано"
    assert orders[0].is_cancelled


class RozetkaDuplicateIncompleteRowStub(RozetkaIncompleteSearchRowStub):
    def request_json(self, method: str, url: str, **kwargs):
        if not url.endswith("/orders/902000003") and kwargs["params"]["types"] == 3:
            copied_kwargs = {
                **kwargs,
                "params": {**kwargs["params"], "types": 2},
            }
            return super().request_json(method, url, **copied_kwargs)
        return super().request_json(method, url, **kwargs)


def test_rozetka_hydrates_duplicate_search_rows_only_once() -> None:
    http = RozetkaDuplicateIncompleteRowStub()
    client = RozetkaClient(
        http,  # type: ignore[arg-type]
        token="test",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )

    orders = client.fetch_orders(datetime(2026, 8, 1, tzinfo=ZoneInfo("Europe/Kyiv")))

    assert http.detail_requests == 1
    assert len(orders) == 1


def test_rozetka_partial_detail_does_not_erase_search_ttn_items_or_status() -> None:
    search = {
        "status": 3,
        "status_data": {"id": 3, "name": "Передано в службу доставки"},
        "delivery": {"ttn": "RMP-787478919", "city": "Київ"},
        "purchases": [{"item_id": "SKU"}],
    }
    detail = {
        "status": None,
        "status_data": {},
        "delivery": {},
        "purchases": [],
    }

    merged = RozetkaClient._merge_search_and_detail(search, detail)

    assert merged["status"] == 3
    assert merged["status_data"] == {"id": 3, "name": "Передано в службу доставки"}
    assert merged["delivery"] == {"ttn": "RMP-787478919", "city": "Київ"}
    assert merged["purchases"] == [{"item_id": "SKU"}]
    assert not RozetkaClient._has_status_fields(detail)


@pytest.mark.parametrize(
    "raw",
    [
        {"status_group": 1},
        {"status_data": {"status_group": 1}},
    ],
)
def test_rozetka_active_group_without_exact_status_requires_details(
    raw: dict[str, object]
) -> None:
    assert not RozetkaClient._has_status_fields(raw)


class RozetkaMissingSearchStatusStub(RozetkaIncompleteSearchRowStub):
    def request_json(self, method: str, url: str, **kwargs):
        payload = super().request_json(method, url, **kwargs)
        if not url.endswith("/orders/902000003"):
            for order in payload["content"]["orders"]:
                order.pop("status", None)
        return payload


def test_rozetka_recovers_missing_search_status_from_details() -> None:
    client = RozetkaClient(
        RozetkaMissingSearchStatusStub(),  # type: ignore[arg-type]
        token="test",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )

    orders = client.fetch_orders(datetime(2026, 8, 1, tzinfo=ZoneInfo("Europe/Kyiv")))

    assert len(orders) == 1
    assert orders[0].source_status == "Відправлено"


def test_rozetka_completed_order_uses_earlier_shipped_history_at_month_boundary() -> None:
    client = RozetkaClient(
        RozetkaSearchStub(),  # type: ignore[arg-type]
        token="test",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )
    raw = {
        "id": "902000005",
        "created": "2026-08-30 10:00:00",
        "changed": "2026-09-01 12:00:00",
        "status": 30,
        "status_group": 2,
        "delivery": {"ttn": "RMP-787478921"},
        "purchases": [
            {"item_id": "SKU", "item_name": "Товар", "quantity": 1, "price": 999}
        ],
        "cost": 999,
        "order_status_history": [
            {"status_id": 3, "created": "2026-08-31 23:50:00"},
            {"status_id": 30, "created": "2026-09-01 12:00:00"},
        ],
    }

    order = client._normalize(raw, source_status="Виконано")

    assert order.completed_at.strftime("%d.%m.%Y %H:%M") == "31.08.2026 23:50"
    assert order.completion_is_exact


@pytest.mark.parametrize("name", ["Скасовано", "Відмова від замовлення", "Отменен"])
def test_rozetka_terminal_group_cancellation_is_not_completed(name: str) -> None:
    raw = {"status_group": 2, "status_data": {"name": name}}

    assert RozetkaClient._eligible_source_status(raw) == "Скасовано"


class RozetkaCompletedHistoryDetailStub:
    def __init__(self) -> None:
        self.detail_requests = 0

    def request_json(self, method: str, url: str, **kwargs):
        base = {
            "id": "902000006",
            "created": "2026-08-30 10:00:00",
            "changed": "2026-09-01 12:00:00",
            "status": 30,
            "status_group": 2,
            "status_data": {"id": 30, "name": "Виконано", "status_group": 2},
            "current_seller_comment": "готово",
            "delivery": {"ttn": "RMP-787478922"},
            "purchases": [
                {"item_id": "SKU", "item_name": "Товар", "quantity": 1, "price": 999}
            ],
            "cost": 999,
        }
        if url.endswith("/orders/902000006"):
            self.detail_requests += 1
            return {
                "success": True,
                "content": {
                    **base,
                    "order_status_history": [
                        {"status_id": 3, "created": "2026-08-31 23:50:00"},
                        {"status_id": 30, "created": "2026-09-01 12:00:00"},
                    ],
                },
            }
        orders = [base] if kwargs["params"]["types"] == 3 else []
        return {
            "success": True,
            "content": {"orders": orders, "_meta": {"pageCount": 1}},
        }


def test_rozetka_fetches_missing_history_for_completed_month_boundary_order() -> None:
    http = RozetkaCompletedHistoryDetailStub()
    client = RozetkaClient(
        http,  # type: ignore[arg-type]
        token="test",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )

    orders = client.fetch_orders(datetime(2026, 8, 1, tzinfo=ZoneInfo("Europe/Kyiv")))

    assert http.detail_requests == 1
    assert len(orders) == 1
    assert orders[0].source_status == "Виконано"
    assert orders[0].completed_at.strftime("%d.%m.%Y %H:%M") == "31.08.2026 23:50"


def test_rozetka_duplicate_merge_prioritizes_cancellation_and_exact_transition() -> None:
    client = RozetkaClient(
        RozetkaSearchStub(),  # type: ignore[arg-type]
        token="test",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )
    base = {
        "id": "902000007",
        "created": "2026-08-30 10:00:00",
        "delivery": {"ttn": "RMP-787478923"},
        "purchases": [
            {"item_id": "SKU", "item_name": "Товар", "quantity": 1, "price": 999}
        ],
        "cost": 999,
    }
    observed = datetime(2026, 8, 31, 10, 0, tzinfo=ZoneInfo("Europe/Kyiv"))
    shipped_observed = client._normalize(
        base, source_status="Відправлено", observed_at=observed
    )
    cancelled_exact = client._normalize(
        {**base, "status_changed_at": "2026-08-31 11:00:00"},
        source_status="Скасовано",
        observed_at=observed,
    )

    merged = client._merge_order_versions(shipped_observed, cancelled_exact)

    assert merged.source_status == "Скасовано"
    assert merged.completed_at.strftime("%d.%m.%Y %H:%M") == "31.08.2026 11:00"
    assert merged.completion_is_exact


class RozetkaDetailCommentStub:
    def request_json(self, method: str, url: str, **kwargs):
        if url.endswith("/orders/902000002"):
            return {
                "success": True,
                "content": {
                    "id": "902000002",
                    "current_seller_comment": "Предоплата 700 грн.",
                },
            }
        orders = []
        if kwargs["params"]["types"] == 2:
            orders = [
                {
                    "id": "902000002",
                    "created": "2026-08-12 10:00:00",
                    "changed": "2026-08-13 09:15:00",
                    "status_data": {"name": "Відправлено"},
                    "cost": "1200",
                    "ttn": "RMP-123456780",
                    "purchases": [
                        {"item_id": "123", "item_name": "Товар", "quantity": 1, "price": 1200}
                    ],
                }
            ]
        return {"success": True, "content": {"orders": orders, "_meta": {"pageCount": 1}}}


def test_rozetka_fetches_details_when_search_omits_seller_comment() -> None:
    client = RozetkaClient(
        RozetkaDetailCommentStub(),  # type: ignore[arg-type]
        token="test",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )

    orders = client.fetch_orders(datetime(2026, 8, 1, tzinfo=ZoneInfo("Europe/Kyiv")))

    assert len(orders) == 1
    assert orders[0].prepayment == Decimal(700)
    assert orders[0].payment_method == "смешанная"
