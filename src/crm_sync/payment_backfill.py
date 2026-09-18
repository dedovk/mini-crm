from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from crm_sync.clients.google_sheets import GoogleSheetsGateway
from crm_sync.clients.http import HttpClient
from crm_sync.clients.prom import PromClient
from crm_sync.config import ConfigurationError, Settings
from crm_sync.models import Order, PaymentBackfillResult

LOGGER = logging.getLogger(__name__)


def _backfill_days() -> int:
    """Read a bounded historical window explicitly requested by the operator."""
    raw = os.getenv("PAYMENT_BACKFILL_DAYS", "0").strip()
    try:
        days = int(raw)
    except ValueError as exc:
        raise ConfigurationError("PAYMENT_BACKFILL_DAYS must be an integer") from exc
    if not 1 <= days <= 730:
        raise ConfigurationError("PAYMENT_BACKFILL_DAYS must be between 1 and 730")
    return days


def _fetch_historical_orders(
    prom: PromClient,
    *,
    until: datetime,
    days: int,
    chunk_days: int = 30,
) -> list[Order]:
    """Fetch long history in retryable chunks and deduplicate by sync key."""
    beginning = until - timedelta(days=days)
    chunk_start = beginning
    by_key: dict[str, Order] = {}
    while chunk_start < until:
        chunk_end = min(chunk_start + timedelta(days=chunk_days), until)
        chunk = prom.fetch_orders_between(chunk_start, chunk_end, payment_only=True)
        LOGGER.info(
            "Prom payment backfill fetched %s order(s) through %s", len(chunk), chunk_end.date()
        )
        for order in chunk:
            by_key[order.sync_key.casefold()] = order
        chunk_start = chunk_end
    return list(by_key.values())


def _require_prom_token(token: str) -> None:
    """Fail maintenance runs instead of silently reporting zero corrections."""
    if not token.strip():
        raise ConfigurationError("PROM_API_TOKEN is required for payment backfill")


def _expected_order_ids() -> set[str]:
    """Read optional order IDs whose authoritative paid state must be present."""
    raw = os.getenv("PAYMENT_BACKFILL_ORDER_IDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _write_summary(*, dry_run: bool, days: int, result: PaymentBackfillResult) -> None:
    summary_path = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return
    lines = [
        "## Prom payment backfill",
        "",
        f"- Mode: {'dry run' if dry_run else 'production'}",
        f"- History window: {days} days",
        f"- Orders corrected: {result.order_updates}",
        f"- Cells corrected: {result.cell_updates}",
        f"- Prom orders in sheet: {result.sheet_order_count}",
        f"- Authoritative paid API candidates: {result.authoritative_candidates}",
        f"- Confirmed paid API matches: {result.api_order_matches}",
        f"- Sheet Prom orders without confirmed paid match: {result.unmatched_sheet_orders}",
        f"- Required order IDs missing: {', '.join(result.missing_expected_order_ids) or 'none'}",
        f"- Backup: {result.backup_name or 'not created'}",
        "",
    ]
    with Path(summary_path).open("a", encoding="utf-8") as summary:
        summary.write("\n".join(lines))


def main() -> int:
    """Correct historical COD labels only for confirmed settled Prom payments."""
    try:
        settings = Settings.from_env()
        logging.basicConfig(
            level=getattr(logging, settings.log_level, logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        days = _backfill_days()
        _require_prom_token(settings.prom_token)
        now = datetime.now(ZoneInfo(settings.timezone))
        http = HttpClient(timeout=settings.http_timeout, max_retries=settings.http_max_retries)
        prom = PromClient(
            http,
            token=settings.prom_token,
            base_url=settings.prom_base_url,
            timezone=settings.timezone,
            installment_fallback_rate=settings.prom_installment_fallback_rate,
        )
        sheets = GoogleSheetsGateway(
            credentials_info=settings.google_service_account_info,
            spreadsheet_id=settings.google_spreadsheet_id,
            worksheet_name=settings.google_worksheet_name,
            header_row=settings.google_header_row,
            sender_options=settings.sender_options,
            timeout=settings.http_timeout,
        )
        orders = _fetch_historical_orders(prom, until=now, days=days)
        expected_order_ids = _expected_order_ids()
        preview = sheets.backfill_prom_payments(
            orders,
            observed_at=now,
            apply_changes=False,
            expected_order_ids=expected_order_ids,
        )
        if preview.authoritative_candidates == 0:
            raise RuntimeError("Prom returned no authoritative paid payment candidates")
        if preview.missing_expected_order_ids:
            missing = ", ".join(preview.missing_expected_order_ids)
            raise RuntimeError(f"Required Prom order IDs were not confirmed as paid: {missing}")
        result = preview
        if not settings.dry_run:
            result = sheets.backfill_prom_payments(
                orders,
                observed_at=now,
                apply_changes=True,
                expected_order_ids=expected_order_ids,
            )
        LOGGER.info(
            "Prom payment backfill checked %s API order(s), corrected %s order(s) and %s cell(s)",
            len(orders),
            result.order_updates,
            result.cell_updates,
        )
        _write_summary(dry_run=settings.dry_run, days=days, result=result)
        return 0
    except ConfigurationError as exc:
        logging.basicConfig(level=logging.ERROR)
        LOGGER.error("Configuration error: %s", exc)
        return 2
    except Exception:
        LOGGER.exception("Prom payment backfill failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
