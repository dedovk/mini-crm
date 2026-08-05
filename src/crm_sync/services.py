from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol
from zoneinfo import ZoneInfo

from crm_sync.clients.google_sheets import GoogleSheetsGateway
from crm_sync.clients.nova_poshta import NovaPoshtaClient
from crm_sync.models import Order

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncResult:
    dry_run: bool
    source_orders: dict[str, int]
    failed_sources: tuple[str, ...]
    warnings: tuple[str, ...]
    fetched_orders: int
    new_orders: int
    stale_orders: int
    pending_shipments: int
    shipment_statuses: int
    refreshed_cells: int = 0
    expense_updates: int = 0
    status_updates: int = 0
    appended_rows: int = 0


class SourceSyncError(RuntimeError):
    """One or more order sources failed after the remaining work was completed."""

    def __init__(self, result: SyncResult) -> None:
        self.result = result
        names = ", ".join(result.failed_sources)
        super().__init__(f"Order source synchronization failed: {names}")


class OrderSource(Protocol):
    source: str

    def fetch_orders(self, since: datetime) -> list[Order]: ...


class OrderExpenseSource(Protocol):
    source: str

    def fetch_expenses(self, since: datetime) -> dict[str, Decimal]: ...


class SyncService:
    def __init__(
        self,
        *,
        sheets: GoogleSheetsGateway,
        nova_poshta: NovaPoshtaClient,
        sources: list[OrderSource],
        expense_source: OrderExpenseSource | None = None,
        timezone: str,
        lookback_days: int,
        new_order_max_age_days: int = 7,
        expense_lookback_days: int = 45,
        sender_default: str,
        dry_run: bool,
    ) -> None:
        self.sheets = sheets
        self.nova_poshta = nova_poshta
        self.sources = sources
        self.expense_source = expense_source
        self.timezone = timezone
        self.lookback_days = lookback_days
        self.new_order_max_age_days = new_order_max_age_days
        self.expense_lookback_days = expense_lookback_days
        self.sender_default = sender_default
        self.dry_run = dry_run

    def run(self) -> SyncResult:
        now = datetime.now(ZoneInfo(self.timezone))
        self.sheets.ensure_schema(apply_changes=not self.dry_run)
        existing_keys = self.sheets.read_existing_sync_keys()
        since = now - timedelta(days=self.lookback_days)

        fetched: list[Order] = []
        failed_sources: list[str] = []
        source_counts: dict[str, int] = {}
        warnings: list[str] = []
        for source in self.sources:
            try:
                source_orders = source.fetch_orders(since)
            except Exception:
                LOGGER.exception("%s source failed; other sources will continue", source.source)
                failed_sources.append(source.source)
                continue
            source_counts[source.source] = len(source_orders)
            LOGGER.info("%s returned %s eligible order(s)", source.source, len(source_orders))
            fetched.extend(source_orders)

        expenses: dict[str, Decimal] | None = None
        if self.expense_source:
            try:
                expense_since = now - timedelta(days=self.expense_lookback_days)
                expenses = self.expense_source.fetch_expenses(expense_since)
                LOGGER.info(
                    "%s finance returned expenses for %s order(s)",
                    self.expense_source.source,
                    len(expenses),
                )
            except Exception as exc:
                warning = (
                    f"{self.expense_source.source} finance is unavailable; "
                    f"existing sheet values were preserved: {exc}"
                )
                LOGGER.warning(
                    "%s finance sync is unavailable; existing sheet values are preserved: %s",
                    self.expense_source.source,
                    exc,
                )
                warnings.append(warning)

        refreshed = 0
        if not self.dry_run:
            refreshed = self.sheets.refresh_order_details(fetched)
        pending_ttns = self.sheets.pending_tracking_numbers()

        unique_orders: list[Order] = []
        run_keys: set[str] = set()
        stale_orders = 0
        new_order_cutoff = (now - timedelta(days=self.new_order_max_age_days)).date()
        for order in fetched:
            key = order.sync_key.casefold()
            if key in existing_keys or key in run_keys:
                continue
            effective_day = (
                order.completed_at.date() if order.completion_is_exact else order.created_at.date()
            )
            if effective_day < new_order_cutoff:
                stale_orders += 1
                continue
            run_keys.add(key)
            unique_orders.append(order)

        if stale_orders:
            LOGGER.warning(
                "Skipped %s previously unseen order(s) older than %s; deep runs only refresh existing orders",
                stale_orders,
                new_order_cutoff.isoformat(),
            )

        all_ttns = list(dict.fromkeys([*pending_ttns, *(order.tracking_number for order in unique_orders)]))
        statuses = self.nova_poshta.get_statuses(all_ttns)
        LOGGER.info("Nova Poshta returned %s shipment status(es)", len(statuses))

        if self.dry_run:
            LOGGER.info(
                "Dry run completed: %s new order(s), %s existing shipment(s) eligible for update",
                len(unique_orders),
                len(pending_ttns),
            )
            result = SyncResult(
                dry_run=True,
                source_orders=source_counts,
                failed_sources=tuple(dict.fromkeys(failed_sources)),
                warnings=tuple(warnings),
                fetched_orders=len(fetched),
                new_orders=len(unique_orders),
                stale_orders=stale_orders,
                pending_shipments=len(pending_ttns),
                shipment_statuses=len(statuses),
                refreshed_cells=refreshed,
            )
            self._raise_for_source_failures(result)
            return result

        updated = self.sheets.update_shipment_statuses(statuses)
        appended_rows = self.sheets.append_orders(
            unique_orders,
            statuses,
            sender_default=self.sender_default,
            operational_day=now.date(),
        )
        expense_updates = 0
        if expenses is not None:
            expense_updates = self.sheets.update_order_expenses(
                expenses,
                source=self.expense_source.source if self.expense_source else "rozetka",
            )
        LOGGER.info(
            "Sync completed: %s detail/formula cell(s) refreshed, %s expense cell(s) updated, %s status cell(s) updated, %s item row(s) appended",
            refreshed,
            expense_updates,
            updated,
            appended_rows,
        )
        result = SyncResult(
            dry_run=False,
            source_orders=source_counts,
            failed_sources=tuple(dict.fromkeys(failed_sources)),
            warnings=tuple(warnings),
            fetched_orders=len(fetched),
            new_orders=len(unique_orders),
            stale_orders=stale_orders,
            pending_shipments=len(pending_ttns),
            shipment_statuses=len(statuses),
            refreshed_cells=refreshed,
            expense_updates=expense_updates,
            status_updates=updated,
            appended_rows=appended_rows,
        )
        self._raise_for_source_failures(result)
        return result

    @staticmethod
    def _raise_for_source_failures(result: SyncResult) -> None:
        if result.failed_sources:
            raise SourceSyncError(result)
