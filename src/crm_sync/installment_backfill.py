from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from crm_sync.clients.google_sheets import GoogleSheetsGateway
from crm_sync.clients.http import HttpClient
from crm_sync.clients.prom import PromClient
from crm_sync.config import ConfigurationError, Settings
from crm_sync.models import InstallmentReconciliationResult
from crm_sync.payment_backfill import fetch_historical_orders, require_prom_token

LOGGER = logging.getLogger(__name__)


def _backfill_days() -> int:
    """Read a bounded historical window for installment reconciliation."""
    raw = os.getenv("INSTALLMENT_BACKFILL_DAYS", "0").strip()
    try:
        days = int(raw)
    except ValueError as exc:
        raise ConfigurationError("INSTALLMENT_BACKFILL_DAYS must be an integer") from exc
    if not 1 <= days <= 730:
        raise ConfigurationError(
            "INSTALLMENT_BACKFILL_DAYS must be between 1 and 730"
        )
    return days


def _write_summary(
    *,
    dry_run: bool,
    days: int,
    result: InstallmentReconciliationResult,
) -> None:
    """Append a concise reconciliation report to the GitHub job summary."""
    summary_path = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return
    lines = [
        "## Prom installment commission reconciliation",
        "",
        f"- Mode: {'dry run' if dry_run else 'production'}",
        f"- History window: {days} days",
        f"- Orders corrected: {result.order_updates}",
        f"- Cells corrected: {result.cell_updates}",
        f"- Exact commissions reported by Prom: {result.reported_orders}",
        f"- Unresolved orders: {', '.join(result.unresolved_orders) or 'none'}",
        f"- Backup: {result.backup_name or 'not created'}",
        "",
    ]
    with Path(summary_path).open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines))


def main() -> int:
    """Reconcile historical Prom installment fees without using estimates."""
    try:
        settings = Settings.from_env()
        logging.basicConfig(
            level=getattr(logging, settings.log_level, logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        days = _backfill_days()
        require_prom_token(settings.prom_token)
        now = datetime.now(ZoneInfo(settings.timezone))
        prom = PromClient(
            HttpClient(
                timeout=settings.http_timeout,
                max_retries=settings.http_max_retries,
            ),
            token=settings.prom_token,
            base_url=settings.prom_base_url,
            timezone=settings.timezone,
        )
        sheets = GoogleSheetsGateway(
            credentials_info=settings.google_service_account_info,
            spreadsheet_id=settings.google_spreadsheet_id,
            worksheet_name=settings.google_worksheet_name,
            header_row=settings.google_header_row,
            sender_options=settings.sender_options,
            timeout=settings.http_timeout,
        )
        reconciliation_order_ids = sheets.installment_reconciliation_order_ids()
        orders = fetch_historical_orders(
            prom,
            until=now,
            days=days,
            include_installment_details=True,
            installment_order_ids=reconciliation_order_ids,
        )
        orders = [
            order
            for order in orders
            if order.external_id in reconciliation_order_ids
        ]
        diagnostics = prom.installment_detail_diagnostics
        if diagnostics.degraded:
            raise RuntimeError(
                "Prom installment detail API is degraded: "
                f"{diagnostics.failures} failure(s), "
                f"{diagnostics.skipped_by_circuit} request(s) skipped"
            )
        result = sheets.reconcile_prom_installments(
            orders,
            observed_at=now,
            apply_changes=not settings.dry_run,
            expected_order_ids=reconciliation_order_ids,
        )
        LOGGER.info(
            "Prom installment reconciliation corrected %s order(s); %s remain unresolved",
            result.order_updates,
            len(result.unresolved_orders),
        )
        _write_summary(dry_run=settings.dry_run, days=days, result=result)
        return 0
    except ConfigurationError as exc:
        logging.basicConfig(level=logging.ERROR)
        LOGGER.error("Configuration error: %s", exc)
        return 2
    except Exception:
        LOGGER.exception("Prom installment reconciliation failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
