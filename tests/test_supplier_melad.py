from decimal import Decimal

import pytest
from requests import Timeout

from crm_sync.clients.supplier_melad import MeladSupplierSheetClient
from crm_sync.models import SupplierCostKey, SupplierCostRecord


class SupplierWorksheetStub:
    def __init__(self, rows: list[list[object]]) -> None:
        self.rows = rows
        self.requests: list[tuple[str, str]] = []

    def get(self, range_name: str, *, value_render_option: str) -> list[list[object]]:
        self.requests.append((range_name, value_render_option))
        return self.rows


def _client(rows: list[list[object]]) -> MeladSupplierSheetClient:
    client = object.__new__(MeladSupplierSheetClient)
    worksheet = SupplierWorksheetStub(rows)
    client._open_worksheet = lambda: worksheet  # type: ignore[method-assign]
    client._max_retries = 0
    client._test_worksheet = worksheet
    return client


def _row(
    tracking: object,
    product: object,
    quantity: object,
    unit_usd: object,
    total_usd: object,
) -> list[object]:
    return [tracking, "", "", "", "", "", product, quantity, unit_usd, total_usd]


def test_fetch_costs_matches_each_item_by_ttn_and_product_code() -> None:
    client = _client(
        [
            _row("20451518006037", 'Палатка надувная "Дом" (PK-ND)', "1 шт", "187,00", 187),
            _row("20451518006037", "Стул-кресло (STD-16)", "2 шт", "8.80", "17.60"),
        ]
    )

    batch = client.fetch_costs()

    assert batch.values == {
        SupplierCostKey("20451518006037", "pknd"): SupplierCostRecord.cost(
            Decimal("187.00"), currency="USD"
        ),
        SupplierCostKey("20451518006037", "std16"): SupplierCostRecord.cost(
            Decimal("8.80"), currency="USD"
        ),
    }
    assert batch.sender == "Melad дроп"
    assert batch.warnings == ()
    assert client._test_worksheet.requests == [("A:J", "UNFORMATTED_VALUE")]


def test_fetch_costs_adds_safe_ttn_alias_for_single_supplier_item() -> None:
    client = _client(
        [_row("20451521990265", "Кораблик-кормушка (REB-0203/MM2010)", 1, 72, 72)]
    )

    batch = client.fetch_costs()

    expected = SupplierCostRecord.cost(Decimal("72"), currency="USD")
    assert batch.values == {
        SupplierCostKey("20451521990265", "reb0203mm2010"): expected,
        SupplierCostKey("20451521990265"): expected,
    }


def test_fetch_costs_does_not_add_ttn_alias_for_multi_item_shipment() -> None:
    client = _client(
        [
            _row("20451496594176", "Стол (STL-MD 106) Место №1", 1, 87, 87),
            _row("20451496594176", "Столик (DASH 64229) Место №2", 1, 16.5, 16.5),
        ]
    )

    batch = client.fetch_costs()

    assert SupplierCostKey("20451496594176") not in batch.values
    assert batch.values == {
        SupplierCostKey("20451496594176", "stlmd106"): SupplierCostRecord.cost(
            Decimal("87"), currency="USD"
        ),
        SupplierCostKey("20451496594176", "dash64229"): SupplierCostRecord.cost(
            Decimal("16.5"), currency="USD"
        ),
    }


def test_fetch_costs_accepts_mixed_case_spaced_product_code() -> None:
    client = _client([_row("20451496594176", "Стол (Stl-MD 106)", 1, 87, 87)])

    batch = client.fetch_costs()

    assert SupplierCostKey("20451496594176", "stlmd106") in batch.values


def test_fetch_costs_does_not_alias_valid_row_when_sibling_is_invalid() -> None:
    client = _client(
        [
            _row("20451496594176", "Стол (STL-MD 106)", 1, 87, 87),
            _row("20451496594176", "Столик (DASH 64229)", 1, "", ""),
        ]
    )

    batch = client.fetch_costs()

    assert SupplierCostKey("20451496594176") not in batch.values
    assert batch.values[SupplierCostKey("20451496594176", "stlmd106")].unit_cost == 87


def test_fetch_costs_removes_unrecognized_product_fallback_for_multi_item_ttn() -> None:
    client = _client(
        [
            _row("20451496594176", "Стіл без артикула", 1, 87, 87),
            _row("20451496594176", "Столик (DASH 64229)", 1, 16.5, 16.5),
        ]
    )

    batch = client.fetch_costs()

    assert SupplierCostKey("20451496594176") not in batch.values
    assert list(batch.values) == [SupplierCostKey("20451496594176", "dash64229")]


def test_fetch_costs_rejects_descriptive_two_word_parentheses_as_sku() -> None:
    client = _client([_row("20451518006037", "Інвертор (Модель 3500W)", 1, 87, 87)])

    batch = client.fetch_costs()

    assert batch.values == {
        SupplierCostKey("20451518006037"): SupplierCostRecord.cost(
            Decimal("87"), currency="USD"
        )
    }


def test_fetch_costs_matches_prefixless_prom_tracking_identifier() -> None:
    client = _client([_row("348784343", "Палатка (PK-ND)", 1, 187, 187)])

    batch = client.fetch_costs()

    expected = SupplierCostRecord.cost(Decimal("187"), currency="USD")
    assert batch.values == {
        SupplierCostKey("prm-348784343", "pknd"): expected,
        SupplierCostKey("prm-348784343"): expected,
    }


def test_fetch_costs_preserves_meest_tracking_instead_of_treating_it_as_prm() -> None:
    client = _client([_row("123-456789", "Палатка (PK-ND)", 1, 187, 187)])

    batch = client.fetch_costs()

    expected = SupplierCostRecord.cost(Decimal("187"), currency="USD")
    assert batch.values == {
        SupplierCostKey("123-456789", "pknd"): expected,
        SupplierCostKey("123-456789"): expected,
    }


def test_fetch_costs_skips_total_mismatch_and_empty_api_response() -> None:
    mismatch = _client([_row("20451518006037", "Стул (STD-16)", 2, 8.8, 99)])
    empty = _client([])

    mismatch_batch = mismatch.fetch_costs()
    empty_batch = empty.fetch_costs()

    assert mismatch_batch.values == {}
    assert "does not match" in mismatch_batch.warnings[0]
    assert mismatch_batch.degraded
    assert empty_batch.values == {}
    assert empty_batch.degraded
    assert empty_batch.warnings == ("Melad supplier sheet returned no rows",)


def test_fetch_costs_uses_generic_ttn_key_for_non_sku_parentheses() -> None:
    client = _client([_row("20451518006037", "Перетворювач (3500W ЧИСТИЙ СИНУС)", 1, 87, 87)])

    batch = client.fetch_costs()

    assert batch.values == {
        SupplierCostKey("20451518006037"): SupplierCostRecord.cost(Decimal("87"), currency="USD")
    }


def test_fetch_costs_quarantines_conflicting_duplicate_item() -> None:
    client = _client(
        [
            _row("20451518006037", "Стул (STD-16)", 1, 8.8, 8.8),
            _row("20451518006037", "Стул (STD-16)", 1, 9.1, 9.1),
        ]
    )

    batch = client.fetch_costs()

    assert batch.values == {}
    assert "conflicting USD costs" in batch.warnings[0]


def test_fetch_costs_ignores_non_numeric_and_negative_costs() -> None:
    client = _client(
        [
            _row("20451518006037", "Стул (STD-16)", 1, "", ""),
            _row("20451518006038", "Стул (STD-17)", 1, -5, -5),
        ]
    )

    batch = client.fetch_costs()

    assert batch.values == {}
    assert len(batch.warnings) == 3  # two row warnings plus degraded schema warning


def test_fetch_costs_propagates_network_error_after_retry_budget() -> None:
    client = object.__new__(MeladSupplierSheetClient)
    client._max_retries = 0

    def fail() -> None:
        raise Timeout("supplier timeout")

    client._open_worksheet = fail  # type: ignore[method-assign]

    with pytest.raises(Timeout, match="supplier timeout"):
        client.fetch_costs()
