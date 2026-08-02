from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from crm_sync.clients.google_sheets import GoogleSheetsGateway
from crm_sync.clients.nova_poshta import NovaPoshtaClient
from crm_sync.models import Order

LOGGER = logging.getLogger(__name__)


class OrderSource(Protocol):
    source: str

    def fetch_orders(self, since: datetime) -> list[Order]: ...


class SyncService:
    def __init__(
        self,
        *,
        sheets: GoogleSheetsGateway,
        nova_poshta: NovaPoshtaClient,
        sources: list[OrderSource],
        timezone: str,
        lookback_days: int,
        sender_default: str,
        dry_run: bool,
    ) -> None:
        self.sheets = sheets
        self.nova_poshta = nova_poshta
        self.sources = sources
        self.timezone = timezone
        self.lookback_days = lookback_days
        self.sender_default = sender_default
        self.dry_run = dry_run

    def run(self) -> None:
        self.sheets.ensure_schema(apply_changes=not self.dry_run)
        existing_keys = self.sheets.read_existing_sync_keys()
        pending_ttns = self.sheets.pending_tracking_numbers()
        since = datetime.now(ZoneInfo(self.timezone)) - timedelta(days=self.lookback_days)

        fetched: list[Order] = []
        for source in self.sources:
            try:
                source_orders = source.fetch_orders(since)
            except Exception:
                LOGGER.exception("%s source failed; other sources will continue", source.source)
                continue
            LOGGER.info("%s returned %s eligible order(s)", source.source, len(source_orders))
            fetched.extend(source_orders)

        unique_orders: list[Order] = []
        run_keys: set[str] = set()
        for order in fetched:
            key = order.sync_key.casefold()
            if key in existing_keys or key in run_keys:
                continue
            run_keys.add(key)
            unique_orders.append(order)

        all_ttns = list(dict.fromkeys([*pending_ttns, *(order.tracking_number for order in unique_orders)]))
        statuses = self.nova_poshta.get_statuses(all_ttns)
        LOGGER.info("Nova Poshta returned %s shipment status(es)", len(statuses))

        if self.dry_run:
            LOGGER.info(
                "Dry run completed: %s new order(s), %s existing shipment(s) eligible for update",
                len(unique_orders),
                len(pending_ttns),
            )
            return

        updated = self.sheets.update_shipment_statuses(statuses)
        appended_rows = self.sheets.append_orders(
            unique_orders,
            statuses,
            sender_default=self.sender_default,
        )
        LOGGER.info("Sync completed: %s status cell(s) updated, %s item row(s) appended", updated, appended_rows)
