import pytest

from crm_sync.clients.http import ApiError
from crm_sync.clients.nova_poshta import NovaPoshtaClient


class StubHttpClient:
    def __init__(self) -> None:
        self.documents: list[dict[str, str]] = []

    def request_json(self, method: str, url: str, **kwargs):
        self.documents = kwargs["json"]["methodProperties"]["Documents"]
        return {
            "success": True,
            "data": [
                {
                    "Number": "20451234567890",
                    "Status": "Відправлення у дорозі",
                    "StatusCode": "5",
                    "RedeliverySum": "3449.00",
                }
            ],
        }


def test_nova_poshta_filters_invalid_tracking_values() -> None:
    http = StubHttpClient()
    client = NovaPoshtaClient(
        http,  # type: ignore[arg-type]
        api_key="test",
        url="https://example.test/v2.0/json/",
    )

    statuses = client.get_statuses(
        ["invalid", "123", "ТТН 20451234567890", "20451234567890"]
    )

    assert http.documents == [{"DocumentNumber": "20451234567890"}]
    assert statuses["20451234567890"].status_code == "5"
    assert statuses["20451234567890"].status == "Прямує до покупця"
    assert str(statuses["20451234567890"].redelivery_sum) == "3449.00"


def test_nova_poshta_uses_fallback_for_blank_status() -> None:
    http = StubHttpClient()
    http.request_json = lambda *args, **kwargs: {
        "success": True,
        "data": [{"Number": "20451234567890", "Status": "   "}],
    }
    client = NovaPoshtaClient(
        http,  # type: ignore[arg-type]
        api_key="test",
        url="https://example.test/v2.0/json/",
    )

    statuses = client.get_statuses(["20451234567890"])

    assert statuses["20451234567890"].status == "Невідомо"


def test_nova_poshta_rejects_malformed_tracking_collection() -> None:
    http = StubHttpClient()
    http.request_json = lambda *args, **kwargs: {"success": True, "data": {}}
    client = NovaPoshtaClient(
        http,  # type: ignore[arg-type]
        api_key="test",
        url="https://example.test/v2.0/json/",
    )

    with pytest.raises(ApiError, match="all batches"):
        client.get_statuses(["20451234567890"])


def test_nova_poshta_keeps_successful_batches_when_later_batch_fails() -> None:
    class PartialHttpClient:
        def __init__(self) -> None:
            self.calls = 0

        def request_json(self, method, url, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise ApiError("temporary outage")
            document = kwargs["json"]["methodProperties"]["Documents"][0]
            return {
                "success": True,
                "data": [{"Number": document["DocumentNumber"], "Status": "Отримано"}],
            }

    client = NovaPoshtaClient(
        PartialHttpClient(),  # type: ignore[arg-type]
        api_key="test",
        url="https://example.test/v2.0/json/",
    )
    numbers = [f"2045{index:010d}" for index in range(101)]

    statuses = client.get_statuses(numbers)

    assert len(statuses) == 1
    assert statuses[numbers[0]].status == "Отримано"
