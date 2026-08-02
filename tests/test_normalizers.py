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


def test_prom_normalizer_uses_product_id_as_product_code() -> None:
    client = PromClient(
        HttpClient(max_retries=0),
        token="test",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )

    order = client._normalize(
        {
            "id": 1,
            "date_created": "2026-08-02 12:00:00",
            "delivery_provider_data": {
                "declaration_number": "ЕН 20 4515 0157 2223",
                "recipient_address": "м. Київ, Відділення №1",
            },
            "delivery_recipient": {
                "last_name": "Тестовий",
                "first_name": "Отримувач",
            },
            "full_price": 100,
            "products": [
                {
                    "id": "608037110",
                    "sku": "5110513990",
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
    assert order.items[0].product_code == "608037110"


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
            "date_added": "2026-08-01 12:00:00",
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
