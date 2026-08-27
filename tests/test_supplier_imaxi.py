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


def test_fetch_costs_preserves_supplier_text_markers_and_typos() -> None:
    client = _client(
        [
            _row("20450420294443", "предопата"),
            _row("20450453411783", "замена"),
            _row("20450511605944", "  Інший текст  "),
        ]
    )

    batch = client.fetch_costs()

    assert batch.values == {
        "20450420294443": SupplierCostRecord.text("предопата"),
        "20450453411783": SupplierCostRecord.text("замена"),
        "20450511605944": SupplierCostRecord.text("Інший текст"),
    }
    assert batch.warnings == ()


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


def test_fetch_costs_skips_empty_values_but_preserves_invalid_and_negative_markers() -> None:
    client = _client(
        [
            _row("2045146814374", ""),
            _row("2045148691245", "невідомо"),
            _row("2045148692927", "-10"),
            _row("not-a-tracking-number", 100),
        ]
    )

    batch = client.fetch_costs()

    assert batch.values == {
        "2045148691245": SupplierCostRecord.text("невідомо"),
        "2045148692927": SupplierCostRecord.text("-10"),
    }
    assert batch.warnings == ()
    assert not batch.degraded


def test_fetch_costs_accepts_empty_google_response() -> None:
    client = _client([])

    batch = client.fetch_costs()

    assert batch.values == {}
    assert not batch.degraded


def test_fetch_costs_quarantines_text_exceeding_sheet_cell_limit() -> None:
    client = _client([_row("20450420294443", "x" * 50_001)])

    batch = client.fetch_costs()

    assert batch.values == {}
    assert "50000-character" in batch.warnings[0]
    assert batch.degraded


def test_supplier_client_construction_does_not_open_optional_sheet() -> None:
    client = ImaxiSupplierSheetClient(
        credentials_info={},
        spreadsheet_id="unavailable-sheet",
        max_retries=0,
    )

    assert client._spreadsheet_id == "unavailable-sheet"
