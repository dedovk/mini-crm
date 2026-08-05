from __future__ import annotations

import os
from pathlib import Path

from crm_sync.services import SyncResult


def render_github_summary(result: SyncResult) -> str:
    outcome = "Failed" if result.failed_sources else "Completed"
    mode = "Dry run" if result.dry_run else "Production"
    lines = [
        "# Marketplace CRM synchronization",
        "",
        f"**{outcome} · {mode}**",
        "",
        "## Order sources",
        "",
        "| Source | Eligible orders | Status |",
        "|---|---:|---|",
    ]
    source_names = list(result.source_orders)
    source_names.extend(source for source in result.failed_sources if source not in result.source_orders)
    for source in source_names:
        failed = source in result.failed_sources
        count = result.source_orders.get(source, 0)
        lines.append(f"| {source} | {count} | {'Failed' if failed else 'OK'} |")

    lines.extend(
        [
            "",
            "## Results",
            "",
            "| Metric | Count |",
            "|---|---:|",
            f"| Eligible orders fetched | {result.fetched_orders} |",
            f"| New orders | {result.new_orders} |",
            f"| Stale unseen orders skipped | {result.stale_orders} |",
            f"| Existing shipments checked | {result.pending_shipments} |",
            f"| Shipment statuses received | {result.shipment_statuses} |",
            f"| Detail/formula cells refreshed | {result.refreshed_cells} |",
            f"| Expense cells updated | {result.expense_updates} |",
            f"| Shipment status cells updated | {result.status_updates} |",
            f"| Item rows appended | {result.appended_rows} |",
            f"| Audit events written | {result.audit_events} |",
        ]
    )
    if result.warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in result.warnings)
    return "\n".join(lines) + "\n"


def write_github_summary(result: SyncResult, path: str | None = None) -> bool:
    destination = path or os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not destination:
        return False
    summary_path = Path(destination)
    try:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        with summary_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(render_github_summary(result))
    except OSError:
        return False
    return True
