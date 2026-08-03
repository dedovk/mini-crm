from datetime import datetime

import pytest

from crm_sync.services import SourceSyncError, SyncService


class SheetsStub:
    def ensure_schema(self, *, apply_changes: bool) -> None:
        assert not apply_changes

    def read_existing_sync_keys(self) -> set[str]:
        return set()

    def pending_tracking_numbers(self) -> list[str]:
        return []


class NovaPoshtaStub:
    def get_statuses(self, tracking_numbers: list[str]) -> dict[str, str]:
        assert tracking_numbers == []
        return {}


class FailingSource:
    source = "prom"

    def fetch_orders(self, since: datetime):
        raise RuntimeError("rate limited")


class SuccessfulSource:
    source = "rozetka"

    def __init__(self) -> None:
        self.called = False

    def fetch_orders(self, since: datetime):
        self.called = True
        return []


def test_source_failure_is_reported_after_other_sources_continue() -> None:
    successful = SuccessfulSource()
    service = SyncService(
        sheets=SheetsStub(),  # type: ignore[arg-type]
        nova_poshta=NovaPoshtaStub(),  # type: ignore[arg-type]
        sources=[FailingSource(), successful],  # type: ignore[list-item]
        timezone="Europe/Kyiv",
        lookback_days=7,
        sender_default="-",
        dry_run=True,
    )

    with pytest.raises(SourceSyncError, match="prom"):
        service.run()

    assert successful.called
