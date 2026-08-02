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
