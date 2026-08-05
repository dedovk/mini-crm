from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from crm_sync.clients.http import HttpClient
from crm_sync.clients.opencart import OpenCartClient
from crm_sync.clients.prom import PromClient
from crm_sync.clients.rozetka import RozetkaClient


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


def test_rozetka_normalizer_prefers_current_status_history_timestamp() -> None:
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

    assert order.completed_at.strftime("%d.%m.%Y %H:%M") == "02.08.2026 12:34"
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
    assert order.sync_key == "opencart:501"
    assert order.payment_method == "смешанная"
    assert order.items[0].product_code == "10"
    assert order.completed_at.strftime("%d.%m.%Y %H:%M") == "03.08.2026 14:25"


def test_opencart_completion_accepts_boolean_text_and_localized_statuses() -> None:
    assert OpenCartClient._is_completed({"is_completed": "true"}) is True
    assert OpenCartClient._is_completed({"order_status": "Виконано"}) is True
    assert OpenCartClient._is_completed({"order_status": "Очікує обробки"}) is False


class RozetkaSearchStub:
    def __init__(self) -> None:
        self.params: dict = {}

    def request_json(self, method: str, url: str, **kwargs):
        self.params = kwargs["params"]
        return {"success": True, "content": {"orders": [], "_meta": {"pageCount": 1}}}


def test_rozetka_search_requests_only_successfully_completed_orders() -> None:
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
    assert http.params["types"] == 3
