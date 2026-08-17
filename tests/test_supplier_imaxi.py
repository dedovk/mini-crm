from decimal import Decimal

from crm_sync.clients.supplier_imaxi import ImaxiSupplierSheetClient
from crm_sync.models import SupplierCostRecord


class SupplierWorksheetStub:
    def __init__(self, rows: list[list[object]]) -> None:
        self.rows = rows
        self.requests: list[tuple[str, str]] = []

    def get(self, range_name: str, *, value_render_option: str) -> list[list[object]]:
        self.requests.append((range_name, value_render_option))
        return self.rows


def _client(rows: list[list[object]]) -> ImaxiSupplierSheetClient:
    client = object.__new__(ImaxiSupplierSheetClient)
    worksheet = SupplierWorksheetStub(rows)
    client._open_worksheet = lambda: worksheet  # type: ignore[method-assign]
    client._max_retries = 0
    client._test_worksheet = worksheet
    return client


def _row(tracking: object, cost: object) -> list[object]:
    return [tracking, "", "", "", "", "", cost]


def test_fetch_costs_reads_supplier_range_once_and_normalizes_values() -> None:
    client = _client(
        [
            _row("2045146814374", "738"),
            _row("RMP-633364474", "Предоплата"),
            _row("05 002 2774 59 19", "1 422,50"),
        ]
    )

    batch = client.fetch_costs()

    assert batch.values == {
        "2045146814374": SupplierCostRecord.cost(Decimal("738")),
        "rmp-633364474": SupplierCostRecord.prepayment(),
        "0500227745919": SupplierCostRecord.cost(Decimal("1422.50")),
    }
    assert batch.warnings == ()
    assert client._test_worksheet.requests == [("L:R", "UNFORMATTED_VALUE")]


def test_fetch_costs_handles_more_than_one_thousand_rows_in_one_request() -> None:
    rows = [_row(f"2045{index:010d}", index) for index in range(1, 1201)]
    client = _client(rows)

    batch = client.fetch_costs()

    assert len(batch.values) == 1200
    assert client._test_worksheet.requests == [("L:R", "UNFORMATTED_VALUE")]


def test_fetch_costs_deduplicates_equal_values_and_skips_conflicts() -> None:
    client = _client(
        [
            _row("2045146814374", 738),
            _row("2045146814374", "738"),
            _row("2045148691245", 724),
            _row("2045148691245", 700),
        ]
    )

    batch = client.fetch_costs()

    assert batch.values == {"2045146814374": SupplierCostRecord.cost(Decimal("738"))}
    assert batch.warnings == (
        "IMAXI TTN 2045148691245 has conflicting costs; it was not imported",
    )


def test_fetch_costs_skips_empty_invalid_and_negative_values() -> None:
    client = _client(
        [
            _row("2045146814374", ""),
            _row("2045148691245", "невідомо"),
            _row("2045148692927", "-10"),
            _row("not-a-tracking-number", 100),
        ]
    )

    batch = client.fetch_costs()

    assert batch.values == {}
    assert len(batch.warnings) == 3
    assert "unsupported cost" in batch.warnings[0]
    assert "non-negative" in batch.warnings[1]
    assert "schema check failed" in batch.warnings[2]
    assert batch.degraded


def test_fetch_costs_accepts_empty_google_response() -> None:
    client = _client([])

    batch = client.fetch_costs()

    assert batch.values == {}
    assert not batch.degraded


def test_supplier_client_construction_does_not_open_optional_sheet() -> None:
    client = ImaxiSupplierSheetClient(
        credentials_info={},
        spreadsheet_id="unavailable-sheet",
        max_retries=0,
    )

    assert client._spreadsheet_id == "unavailable-sheet"
