from pathlib import Path

from crm_sync.reporting import render_github_summary, write_github_summary
from crm_sync.services import SyncResult


def sample_result() -> SyncResult:
    return SyncResult(
        dry_run=False,
        source_orders={"prom": 25, "rozetka": 3, "opencart": 0},
        failed_sources=(),
        warnings=("rozetka finance is unavailable",),
        fetched_orders=28,
        new_orders=2,
        stale_orders=1,
        pending_shipments=5,
        shipment_statuses=5,
        refreshed_cells=4,
        expense_updates=0,
        status_updates=1,
        appended_rows=2,
    )


def test_render_github_summary_contains_sources_metrics_and_warnings() -> None:
    summary = render_github_summary(sample_result())

    assert "**Completed · Production**" in summary
    assert "| prom | 25 | OK |" in summary
    assert "| New orders | 2 |" in summary
    assert "- rozetka finance is unavailable" in summary


def test_write_github_summary_appends_to_requested_file(tmp_path: Path) -> None:
    path = tmp_path / "summary.md"

    assert write_github_summary(sample_result(), str(path)) is True
    assert write_github_summary(sample_result(), str(path)) is True

    content = path.read_text(encoding="utf-8")
    assert content.count("# Marketplace CRM synchronization") == 2
