from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from crm_sync.config import ConfigurationError
from crm_sync.payment_backfill import (
    _backfill_days,
    _expected_order_ids,
    _fetch_historical_orders,
    _require_prom_token,
)


@pytest.mark.parametrize("value", ["1", "120", "730"])
def test_payment_backfill_days_accepts_bounded_integer(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("PAYMENT_BACKFILL_DAYS", value)

    assert _backfill_days() == int(value)


@pytest.mark.parametrize("value", ["", "0", "731", "invalid", "1.5"])
def test_payment_backfill_days_rejects_unsafe_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("PAYMENT_BACKFILL_DAYS", value)

    with pytest.raises(ConfigurationError):
        _backfill_days()


def test_payment_backfill_requires_prom_token() -> None:
    with pytest.raises(ConfigurationError, match="PROM_API_TOKEN"):
        _require_prom_token("  ")


def test_payment_backfill_parses_expected_order_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAYMENT_BACKFILL_ORDER_IDS", "427844867, 428234493,")

    assert _expected_order_ids() == {"427844867", "428234493"}


def test_payment_backfill_fetches_long_history_in_bounded_chunks() -> None:
    class FakeProm:
        def __init__(self) -> None:
            self.calls: list[tuple[datetime, datetime]] = []

        def fetch_orders_between(
            self, since: datetime, until: datetime, *, payment_only: bool = False
        ):
            assert payment_only
            self.calls.append((since, until))
            return [SimpleNamespace(sync_key=f"prom:{len(self.calls)}")]

    prom = FakeProm()
    until = datetime(2026, 9, 18, tzinfo=UTC)

    orders = _fetch_historical_orders(prom, until=until, days=65, chunk_days=30)

    assert len(prom.calls) == 3
    assert all((end - start).days <= 30 for start, end in prom.calls)
    assert len(orders) == 3
