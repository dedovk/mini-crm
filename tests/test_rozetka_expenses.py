from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from crm_sync.clients.rozetka import RozetkaClient


class RozetkaExpenseStub:
    def request_json(self, method: str, url: str, **kwargs):
        params = kwargs.get("params", {})
        if url.endswith("/balances/search-data"):
            return {
                "success": True,
                "content": {
                    "operationTypes": {
                        "2": {"id": 2, "name": "sale_commiss", "title": "Комісія за продаж"}
                    },
                    "operationTypesLogistic": {
                        "34": {"id": 34, "name": "withdrawalLogisticMP", "title": "Доставка відправлення"},
                        "35": {"id": 35, "name": "adjustmentLogisticMPUp", "title": "Коригування рахунку +"},
                    },
                },
            }
        if url.endswith("/balance-logistic/search-data"):
            return {
                "success": True,
                "content": {
                    "operationTypes": [
                        {"id": 34, "title": "Доставка відправлення"},
                        {"id": 35, "title": "Коригування рахунку +"},
                        {"id": 43, "title": "Зворотня доставка відправлення"},
                    ]
                },
            }
        if url.endswith("/balances/search"):
            page = int(params["page"])
            if str(params.get("operationType")) != "2":
                return {
                    "success": True,
                    "content": {
                        "billingLogUserBalances": [
                            {"id": 3001, "operationType": 34, "orderId": 901, "debit": -30, "credit": 0},
                            {"id": 3002, "operationType": 35, "orderId": 901, "debit": 0, "credit": 30},
                            {"id": 3003, "operationType": 34, "orderId": 902, "debit": -30, "credit": 0},
                        ],
                        "_meta": {"pageCount": 1, "currentPage": 1},
                    },
                }
            transaction = {
                "id": 1001,
                "logId": 2001,
                "orderId": 901,
                "operationType": 2,
                "debit": "183,42",
                "credit": 0,
            }
            return {
                "success": True,
                "content": {
                    "billingLogUserBalances": [transaction] if page == 1 else [transaction, {
                        "id": 1002,
                        "orderId": 902,
                        "operationType": 2,
                        "debit": "653.16",
                        "credit": 0,
                    }],
                    "_meta": {"pageCount": 2, "currentPage": page},
                },
            }
        if url.endswith("/balance-logistic/search"):
            return {
                "success": True,
                "content": {
                    "logisticBalances": [
                        {"operation_id": 3001, "operation_type": 34, "order_id": 901, "debit": "-30 грн", "credit": 0},
                        {"operation_id": 3002, "operation_type": 35, "order_id": 901, "debit": 0, "credit": "30 грн"},
                        {"operation_id": 3003, "operation_type": 34, "order_id": 902, "debit": -30, "credit": 0},
                        {"operation_id": 3004, "operation_type": 43, "order_id": 902, "debit": -30, "credit": 0},
                    ],
                    "_meta": {"pageCount": 1, "currentPage": 1},
                },
            }
        raise AssertionError(f"Unexpected request: {url}")


class RozetkaExpenseMetadataDeniedStub(RozetkaExpenseStub):
    def request_json(self, method: str, url: str, **kwargs):
        if url.endswith("/balance-logistic/search-data"):
            return {"success": False, "errors": {"code": 1010, "message": "access_denied"}}
        return super().request_json(method, url, **kwargs)


class RozetkaDedicatedLogisticsDeniedStub(RozetkaExpenseMetadataDeniedStub):
    def request_json(self, method: str, url: str, **kwargs):
        if url.endswith("/balance-logistic/search"):
            return {"success": False, "errors": {"code": 1010, "message": "access_denied"}}
        return super().request_json(method, url, **kwargs)


def test_rozetka_expenses_sum_commission_and_net_logistics_by_order_id() -> None:
    client = RozetkaClient(
        RozetkaExpenseStub(),  # type: ignore[arg-type]
        token="test-token",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )

    expenses = client.fetch_expenses(datetime(2026, 7, 1, tzinfo=ZoneInfo("Europe/Kyiv")))

    assert expenses == {
        "901": Decimal("183.42"),
        "902": Decimal("683.16"),
    }


def test_rozetka_expenses_use_stable_type_ids_when_filter_metadata_is_denied() -> None:
    client = RozetkaClient(
        RozetkaExpenseMetadataDeniedStub(),  # type: ignore[arg-type]
        token="test-token",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )

    expenses = client.fetch_expenses(datetime(2026, 7, 1, tzinfo=ZoneInfo("Europe/Kyiv")))

    assert expenses["901"] == Decimal("183.42")


def test_rozetka_expenses_fall_back_to_general_balance_history_for_logistics() -> None:
    client = RozetkaClient(
        RozetkaDedicatedLogisticsDeniedStub(),  # type: ignore[arg-type]
        token="test-token",
        username="",
        password="",
        base_url="https://example.test",
        timezone="Europe/Kyiv",
    )

    expenses = client.fetch_expenses(datetime(2026, 7, 1, tzinfo=ZoneInfo("Europe/Kyiv")))

    assert expenses == {"901": Decimal("183.42"), "902": Decimal("683.16")}
