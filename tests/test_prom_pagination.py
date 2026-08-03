from datetime import datetime
from zoneinfo import ZoneInfo

from crm_sync.clients.prom import PromClient


class StubHttpClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def request_json(self, method: str, url: str, **kwargs):
        self.calls.append(kwargs["params"])
        if len(self.calls) == 1:
            return {"orders": [prom_order(order_id) for order_id in range(200, 100, -1)]}
        return {"orders": [prom_order(100)]}


def prom_order(order_id: int) -> dict:
    return {
        "id": order_id,
        "status": "delivered",
        "date_created": "2026-08-01T12:00:00",
        "client_first_name": "Клієнт",
        "phone": "0501234567",
        "delivery_data": {"declaration_number": f"2045{order_id:010d}"},
        "full_price": "100.00",
        "products": [
            {
                "name": "Товар",
                "sku": "SKU-1",
                "quantity": 1,
                "price": "100.00",
                "total_price": "100.00",
            }
        ],
    }


def test_prom_uses_last_id_cursor_instead_of_offset() -> None:
    http = StubHttpClient()
    client = PromClient(
        http,  # type: ignore[arg-type]
        token="test",
        base_url="https://example.test/api/v1",
        timezone="Europe/Kyiv",
    )

    orders = client.fetch_orders(datetime(2026, 7, 1, tzinfo=ZoneInfo("Europe/Kyiv")))

    assert len(orders) == 101
    assert len(http.calls) == 2
    assert "offset" not in http.calls[0]
    assert http.calls[0]["status"] == "delivered"
    assert http.calls[1]["last_id"] == 100
    assert http.calls[0]["date_from"].startswith("2026-06-30")
