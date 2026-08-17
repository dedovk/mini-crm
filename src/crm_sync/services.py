from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Mapping, Protocol
from zoneinfo import ZoneInfo

from crm_sync.integrity import IntegrityError, IntegrityReport, validate_incoming_orders
from crm_sync.models import (
    Order,
    OrderAuditEvent,
    ResolvedSupplierCost,
    ShipmentStatus,
    ShipmentStatusChange,
    ShipmentUpdateResult,
    SupplierCostBatch,
    SupplierCostUpdateResult,
    SyncHealthState,
)
from crm_sync.utils import (
    extract_ttn,
    has_prepayment_request,
    parse_prepayment,
    tracking_match_key,
)

LOGGER = logging.getLogger(__name__)
REPAIRABLE_PREFLIGHT_ERROR_PREFIXES = ("repairable formula error at ",)


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
    supplier_cost_updates: int = 0
    status_updates: int = 0
    appended_rows: int = 0
    audit_events: int = 0
    backup_created: str = ""
    layout_advanced: bool = False
    health: SyncHealthState = field(default_factory=SyncHealthState)


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


class SupplierCostSource(Protocol):
    source: str

    def fetch_costs(self) -> SupplierCostBatch: ...


class ShipmentTracker(Protocol):
    def get_statuses(self, tracking_numbers: list[str]) -> dict[str, ShipmentStatus]: ...


class SheetGateway(Protocol):
    def requires_schema_migration(self) -> bool: ...

    def ensure_schema(self, *, apply_changes: bool) -> None: ...

    def validate_integrity(self) -> IntegrityReport: ...

    def read_existing_sync_keys(self) -> set[str]: ...

    def backfill_completion_state(self, *, observed_at: datetime) -> int: ...

    def record_completion_observations(
        self, orders: list[Order], *, observed_at: datetime
    ) -> tuple[OrderAuditEvent, ...]: ...

    def refresh_order_details(self, orders: list[Order]) -> int: ...

    def pending_tracking_numbers(self) -> list[str]: ...

    def update_shipment_statuses(
        self, statuses: dict[str, ShipmentStatus]
    ) -> ShipmentUpdateResult: ...

    def latest_layout_day(self) -> date | None: ...

    def needs_refusal_reconciliation(
        self,
        *,
        supplier_prepayment_tracking_keys: set[str] | None = None,
        quarantine_unverified_refusals: bool = False,
    ) -> bool: ...

    def create_backup(self, *, created_at: datetime) -> str: ...

    def append_orders(
        self,
        orders: list[Order],
        statuses: dict[str, ShipmentStatus],
        *,
        sender_default: str,
        operational_day: date,
        observed_at: datetime,
        force_rebuild: bool,
        excluded_sync_keys: set[str] | None = None,
        supplier_prepayment_tracking_keys: set[str] | None = None,
        quarantine_unverified_refusals: bool = False,
    ) -> int: ...

    def update_order_expenses(self, expenses: dict[str, Decimal], *, source: str) -> int: ...

    def update_supplier_costs(
        self,
        costs: Mapping[str, ResolvedSupplierCost],
        *,
        observed_at: datetime,
    ) -> SupplierCostUpdateResult: ...

    def append_audit_events(self, events: list[OrderAuditEvent]) -> int: ...

    def record_sync_health(
        self, failed_components: list[str], *, occurred_at: datetime
    ) -> SyncHealthState: ...


@dataclass(frozen=True, slots=True)
class SourceBatch:
    orders: tuple[Order, ...]
    counts: dict[str, int]
    failed_sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NewOrderSelection:
    orders: tuple[Order, ...]
    stale_count: int


@dataclass(frozen=True, slots=True)
class SupplierCostFetch:
    batches: tuple[SupplierCostBatch, ...]
    failed_sources: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ResolvedSupplierCosts:
    values: Mapping[str, ResolvedSupplierCost]
    warnings: tuple[str, ...]


class SyncService:
    def __init__(
        self,
        *,
        sheets: SheetGateway,
        nova_poshta: ShipmentTracker,
        sources: list[OrderSource],
        expense_source: OrderExpenseSource | None = None,
        supplier_cost_sources: list[SupplierCostSource] | None = None,
        timezone: str,
        lookback_days: int,
        new_order_max_age_days: int = 7,
        expense_lookback_days: int = 45,
        sender_default: str,
        dry_run: bool,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sheets = sheets
        self.nova_poshta = nova_poshta
        self.sources = sources
        self.expense_source = expense_source
        self.supplier_cost_sources = supplier_cost_sources or []
        self.timezone = timezone
        self.lookback_days = lookback_days
        self.new_order_max_age_days = new_order_max_age_days
        self.expense_lookback_days = expense_lookback_days
        self.sender_default = sender_default
        self.dry_run = dry_run
        self.clock = clock or (lambda: datetime.now(ZoneInfo(self.timezone)))

    def run(self) -> SyncResult:
        now = self.clock()
        schema_migration_required = self.sheets.requires_schema_migration()
        backup_created = ""
        if schema_migration_required and not self.dry_run:
            backup_created = self.sheets.create_backup(created_at=now)
            LOGGER.info(
                "Created Google Sheets backup before schema migration: %s",
                backup_created,
            )
            self.sheets.ensure_schema(apply_changes=True)
        else:
            self.sheets.ensure_schema(apply_changes=False)
        sheet_integrity = self.sheets.validate_integrity()
        repairable_sheet_errors = bool(sheet_integrity.errors) and _only_repairable_preflight_errors(
            sheet_integrity.errors
        )
        if not sheet_integrity.ok and (
            self.dry_run or not repairable_sheet_errors
        ):
            raise IntegrityError(sheet_integrity)
        existing_keys = self.sheets.read_existing_sync_keys()
        source_batch = self._fetch_orders(now - timedelta(days=self.lookback_days))
        fetched_all = list(source_batch.orders)
        cancelled_orders = [order for order in fetched_all if order.is_cancelled]
        cancelled_keys = {order.sync_key.casefold() for order in cancelled_orders}
        cancelled_existing = cancelled_keys & {key.casefold() for key in existing_keys}
        fetched = [
            order
            for order in fetched_all
            if not order.is_cancelled
            or order.prepayment > 0
            or has_prepayment_request(order.note)
            or order.payment_method.strip().casefold() == "смешанная"
        ]
        warnings: list[str] = []

        incoming_integrity = validate_incoming_orders(fetched)
        if not incoming_integrity.ok:
            raise IntegrityError(incoming_integrity)
        warnings.extend(sheet_integrity.warnings)
        if sheet_integrity.errors:
            warnings.append(
                "Sheet contains formula errors before repair; production sync will try to refresh formulas and values."
            )
        warnings.extend(incoming_integrity.warnings)

        expenses, expense_warning = self._fetch_expenses(now)
        if expense_warning:
            warnings.append(expense_warning)
        supplier_costs = self._fetch_supplier_costs()
        warnings.extend(supplier_costs.warnings)
        for batch in supplier_costs.batches:
            warnings.extend(batch.warnings)
        resolved_supplier_costs = self._resolve_supplier_costs(supplier_costs.batches)
        warnings.extend(resolved_supplier_costs.warnings)

        refreshed = 0
        completion_events: tuple[OrderAuditEvent, ...] = ()
        if not self.dry_run:
            if not schema_migration_required:
                self.sheets.ensure_schema(apply_changes=True)
            refreshed += self.sheets.backfill_completion_state(observed_at=now)
        pending_ttns = self.sheets.pending_tracking_numbers()

        new_order_cutoff = (now - timedelta(days=self.new_order_max_age_days)).date()
        selection = self._select_new_orders(fetched, existing_keys, cutoff=new_order_cutoff)
        unique_orders = list(selection.orders)

        if selection.stale_count:
            LOGGER.warning(
                "Skipped %s previously unseen order(s) older than %s; deep runs only refresh existing orders",
                selection.stale_count,
                new_order_cutoff.isoformat(),
            )

        all_ttns = list(
            dict.fromkeys([*pending_ttns, *(order.tracking_number for order in fetched)])
        )
        nova_poshta_failed = False
        try:
            statuses = self.nova_poshta.get_statuses(all_ttns)
        except Exception as exc:  # noqa: BLE001 - optional integration boundary
            nova_poshta_failed = True
            statuses = {}
            warning = (
                "nova-poshta tracking is unavailable; existing shipment statuses "
                f"were preserved: {exc}"
            )
            warnings.append(warning)
            LOGGER.warning(warning)
        else:
            LOGGER.info("Nova Poshta returned %s shipment status(es)", len(statuses))
        inferred_prepayments = self._apply_tracking_prepayments(fetched, statuses)
        if inferred_prepayments:
            LOGGER.info(
                "Inferred prepayment from Nova Poshta COD for %s order(s)",
                inferred_prepayments,
            )

        if not self.dry_run:
            completion_events = self.sheets.record_completion_observations(
                fetched_all,
                observed_at=now,
            )
            refreshed += self.sheets.refresh_order_details(fetched_all)

        if self.dry_run:
            LOGGER.info(
                "Dry run completed: %s new order(s), %s existing shipment(s) eligible for update",
                len(unique_orders),
                len(pending_ttns),
            )
            result = SyncResult(
                dry_run=True,
                source_orders=source_batch.counts,
                failed_sources=source_batch.failed_sources,
                warnings=tuple(warnings),
                fetched_orders=len(fetched),
                new_orders=len(unique_orders),
                stale_orders=selection.stale_count,
                pending_shipments=len(pending_ttns),
                shipment_statuses=len(statuses),
                refreshed_cells=refreshed,
            )
            self._raise_for_source_failures(result)
            return result

        shipment_update = self.sheets.update_shipment_statuses(statuses)
        supplier_cost_update = SupplierCostUpdateResult()
        supplier_cost_write_failed = False
        supplier_audit_count = 0
        supplier_prepayment_tracking_keys = {
            tracking_match_key(tracking)
            for tracking, resolved in resolved_supplier_costs.values.items()
            if resolved.record.kind == "prepayment" and tracking_match_key(tracking)
        }
        quarantine_unverified_refusals = bool(
            self.supplier_cost_sources and supplier_costs.failed_sources
        )
        refused_orders_present = self.sheets.needs_refusal_reconciliation(
            supplier_prepayment_tracking_keys=supplier_prepayment_tracking_keys,
            quarantine_unverified_refusals=quarantine_unverified_refusals,
        )
        latest_layout_day = self.sheets.latest_layout_day()
        layout_advanced = latest_layout_day is None or latest_layout_day < now.date()
        details_changed = refreshed > 0
        if (
            unique_orders
            or layout_advanced
            or refused_orders_present
            or details_changed
            or repairable_sheet_errors
        ) and not backup_created:
            backup_created = self.sheets.create_backup(created_at=now)
            LOGGER.info(
                "Created Google Sheets backup before structural rebuild: %s",
                backup_created,
            )
        appended_rows = self.sheets.append_orders(
            unique_orders,
            statuses,
            sender_default=self.sender_default,
            operational_day=now.date(),
            observed_at=now,
            force_rebuild=(
                layout_advanced
                or refused_orders_present
                or details_changed
                or schema_migration_required
                or repairable_sheet_errors
            ),
            excluded_sync_keys=cancelled_existing,
            supplier_prepayment_tracking_keys=supplier_prepayment_tracking_keys,
            quarantine_unverified_refusals=quarantine_unverified_refusals,
        )
        if resolved_supplier_costs.values:
            try:
                supplier_cost_update = self.sheets.update_supplier_costs(
                    resolved_supplier_costs.values,
                    observed_at=now,
                )
            except Exception as exc:  # noqa: BLE001 - optional integration boundary
                supplier_cost_write_failed = True
                warning = (
                    "supplier cost write is unavailable; existing cost cells were preserved: "
                    f"{exc}"
                )
                warnings.append(warning)
                LOGGER.warning(warning)
        if supplier_cost_update.audit_events:
            try:
                supplier_audit_count = self.sheets.append_audit_events(
                    list(supplier_cost_update.audit_events)
                )
            except Exception as exc:  # noqa: BLE001 - audit must not abort order sync
                warning = f"Google Sheets supplier audit log is unavailable: {exc}"
                warnings.append(warning)
                LOGGER.warning(warning)
        expense_updates = 0
        if expenses is not None:
            expense_updates = self.sheets.update_order_expenses(
                expenses,
                source=self.expense_source.source if self.expense_source else "rozetka",
            )
        completion_status_keys = {
            event.sync_key.casefold()
            for event in completion_events
            if event.field == "Статус замовлення"
        }
        audit_events = self._build_audit_events(
            now=now,
            new_orders=unique_orders,
            completion_events=completion_events,
            shipment_changes=shipment_update.changes,
            cancelled_orders=[
                order
                for order in cancelled_orders
                if order.sync_key.casefold() in cancelled_existing
                and order.sync_key.casefold() in completion_status_keys
            ],
        )
        audit_count = supplier_audit_count
        try:
            audit_count += self.sheets.append_audit_events(audit_events)
        except Exception as exc:  # noqa: BLE001 - audit must not abort order sync
            warning = f"Google Sheets audit log is unavailable: {exc}"
            warnings.append(warning)
            LOGGER.warning(warning)
        post_integrity = self.sheets.validate_integrity()
        if not post_integrity.ok:
            if backup_created:
                LOGGER.error("Post-write integrity failed; recovery copy is %s", backup_created)
            raise IntegrityError(post_integrity)
        warnings.extend(
            warning
            for warning in post_integrity.warnings
            if warning not in warnings
        )
        failed_components = list(source_batch.failed_sources)
        if nova_poshta_failed:
            failed_components.append("nova-poshta")
        failed_components.extend(
            "rozetka-finance"
            for warning in warnings
            if warning.casefold().startswith("rozetka finance is unavailable")
        )
        failed_components.extend(supplier_costs.failed_sources)
        if supplier_cost_write_failed:
            failed_components.append("supplier-cost-write")
        health = self.sheets.record_sync_health(failed_components, occurred_at=now)
        LOGGER.info(
            "Sync completed: %s detail/formula cell(s) refreshed, %s expense cell(s) updated, %s supplier cost cell(s) updated, %s status cell(s) updated, %s item row(s) appended, %s audit event(s) written",
            refreshed,
            expense_updates,
            supplier_cost_update.cell_updates,
            shipment_update.cell_updates,
            appended_rows,
            audit_count,
        )
        result = SyncResult(
            dry_run=False,
            source_orders=source_batch.counts,
            failed_sources=source_batch.failed_sources,
            warnings=tuple(warnings),
            fetched_orders=len(fetched),
            new_orders=len(unique_orders),
            stale_orders=selection.stale_count,
            pending_shipments=len(pending_ttns),
            shipment_statuses=len(statuses),
            refreshed_cells=refreshed,
            expense_updates=expense_updates,
            supplier_cost_updates=supplier_cost_update.cell_updates,
            status_updates=shipment_update.cell_updates,
            appended_rows=appended_rows,
            audit_events=audit_count,
            backup_created=backup_created,
            layout_advanced=layout_advanced,
            health=health,
        )
        # Production failures are aggregated in the persisted health state and
        # escalated through a single GitHub issue after the configured threshold.
        return result

    def _fetch_orders(self, since: datetime) -> SourceBatch:
        fetched: list[Order] = []
        failed_sources: list[str] = []
        counts: dict[str, int] = {}
        for source in self.sources:
            try:
                orders = source.fetch_orders(since)
            except Exception:
                LOGGER.exception("%s source failed; other sources will continue", source.source)
                failed_sources.append(source.source)
                continue
            counts[source.source] = len(orders)
            LOGGER.info("%s returned %s eligible order(s)", source.source, len(orders))
            fetched.extend(orders)
        return SourceBatch(
            orders=tuple(fetched),
            counts=counts,
            failed_sources=tuple(dict.fromkeys(failed_sources)),
        )

    def _fetch_expenses(self, now: datetime) -> tuple[dict[str, Decimal] | None, str]:
        if self.expense_source is None:
            return None, ""
        try:
            since = now - timedelta(days=self.expense_lookback_days)
            expenses = self.expense_source.fetch_expenses(since)
        except Exception as exc:  # noqa: BLE001 - optional integration boundary
            warning = (
                f"{self.expense_source.source} finance is unavailable; "
                f"existing sheet values were preserved: {exc}"
            )
            LOGGER.warning(warning)
            return None, warning
        LOGGER.info(
            "%s finance returned expenses for %s order(s)",
            self.expense_source.source,
            len(expenses),
        )
        return expenses, ""

    def _fetch_supplier_costs(self) -> SupplierCostFetch:
        batches: list[SupplierCostBatch] = []
        failed_sources: list[str] = []
        warnings: list[str] = []
        for source in self.supplier_cost_sources:
            try:
                batch = source.fetch_costs()
            except Exception as exc:  # noqa: BLE001 - optional integration boundary
                warning = (
                    f"{source.source} costs are unavailable; existing sheet values were preserved: "
                    f"{exc}"
                )
                LOGGER.warning(warning)
                warnings.append(warning)
                failed_sources.append(source.source)
                continue
            batches.append(batch)
            if batch.degraded:
                failed_sources.append(batch.source)
        return SupplierCostFetch(
            batches=tuple(batches),
            failed_sources=tuple(dict.fromkeys(failed_sources)),
            warnings=tuple(warnings),
        )

    @staticmethod
    def _resolve_supplier_costs(
        batches: tuple[SupplierCostBatch, ...],
    ) -> ResolvedSupplierCosts:
        values: dict[str, ResolvedSupplierCost] = {}
        owners: dict[str, str] = {}
        conflicted: set[str] = set()
        warnings: list[str] = []
        for batch in batches:
            for raw_tracking, record in batch.values.items():
                tracking = tracking_match_key(raw_tracking)
                if not tracking:
                    warnings.append(
                        f"{batch.source} returned invalid TTN {raw_tracking!r}; it was not imported"
                    )
                    continue
                if tracking in conflicted:
                    continue
                previous = values.get(tracking)
                if previous is not None and previous.record != record:
                    previous_source = owners.pop(tracking)
                    values.pop(tracking)
                    conflicted.add(tracking)
                    warnings.append(
                        f"Supplier TTN {tracking} conflicts between "
                        f"{previous_source} and {batch.source}; it was not imported"
                    )
                    continue
                values[tracking] = ResolvedSupplierCost(source=batch.source, record=record)
                owners[tracking] = batch.source
        return ResolvedSupplierCosts(values=values, warnings=tuple(warnings))

    @staticmethod
    def _select_new_orders(
        fetched: list[Order], existing_keys: set[str], *, cutoff: date
    ) -> NewOrderSelection:
        selected: list[Order] = []
        run_keys: set[str] = set()
        stale_count = 0
        for order in fetched:
            key = order.sync_key.casefold()
            if key in existing_keys or key in run_keys:
                continue
            effective_day = (
                order.updated_at.date()
                if order.source.casefold() == "opencart" and order.updated_at
                else order.completed_at.date()
                if order.completion_is_exact or not order.is_completed
                else order.created_at.date()
            )
            if effective_day < cutoff:
                stale_count += 1
                continue
            run_keys.add(key)
            selected.append(order)
        return NewOrderSelection(tuple(selected), stale_count)

    @staticmethod
    def _apply_tracking_prepayments(
        orders: list[Order], statuses: dict[str, ShipmentStatus]
    ) -> int:
        inferred_count = 0
        for order in orders:
            explicit = order.prepayment or parse_prepayment(order.note)
            if explicit > 0:
                order.prepayment = explicit
                order.payment_method = "смешанная"
                continue

            tracking = extract_ttn(order.tracking_number)
            shipment = statuses.get(tracking)
            if not shipment or shipment.redelivery_sum <= 0:
                continue
            goods_total = sum((item.line_total for item in order.items), Decimal(0))
            inferred = goods_total - shipment.redelivery_sum
            if inferred <= 0:
                continue
            order.prepayment = inferred.quantize(Decimal("0.01"))
            order.payment_method = "смешанная"
            inferred_count += 1
        return inferred_count

    @staticmethod
    def _build_audit_events(
        *,
        now: datetime,
        new_orders: list[Order],
        completion_events: tuple[OrderAuditEvent, ...],
        shipment_changes: tuple[ShipmentStatusChange, ...],
        cancelled_orders: list[Order] | None = None,
    ) -> list[OrderAuditEvent]:
        events = [
            OrderAuditEvent(
                occurred_at=now,
                event_type="Додано замовлення",
                source=order.source,
                order_id=order.external_id,
                sync_key=order.sync_key,
                tracking_number=order.tracking_number,
                new_value="Додано до CRM",
                details=f"Позицій: {len(order.items)}; сума: {order.total}",
            )
            for order in new_orders
        ]
        events.extend(
            OrderAuditEvent(
                occurred_at=now,
                event_type="Змінено статус замовлення",
                source=order.source,
                order_id=order.external_id,
                sync_key=order.sync_key,
                tracking_number=order.tracking_number,
                field="Статус замовлення",
                old_value="Невідомо",
                new_value=order.source_status,
                details=(
                    "Точна дата статусу з API"
                    if order.completion_is_exact
                    else "Перше спостереження статусу синхронізацією"
                ),
            )
            for order in new_orders
        )
        events.extend(completion_events)
        events.extend(
            OrderAuditEvent(
                occurred_at=now,
                event_type="Видалено скасоване замовлення",
                source=order.source,
                order_id=order.external_id,
                sync_key=order.sync_key,
                tracking_number=order.tracking_number,
                field="Статус замовлення джерела",
                old_value="Виконано",
                new_value=order.source_status,
                details="Замовлення виключено з CRM та всіх підсумків",
            )
            for order in (cancelled_orders or [])
        )
        events.extend(
            OrderAuditEvent(
                occurred_at=now,
                event_type="Змінено статус доставки",
                source=change.source,
                order_id=change.order_id,
                sync_key=change.sync_key,
                tracking_number=change.tracking_number,
                field="Статус доставки",
                old_value=change.old_status,
                new_value=change.new_status,
            )
            for change in shipment_changes
        )
        return events

    @staticmethod
    def _raise_for_source_failures(result: SyncResult) -> None:
        if result.failed_sources:
            raise SourceSyncError(result)


def _only_repairable_preflight_errors(errors: tuple[str, ...]) -> bool:
    return bool(errors) and all(
        error.startswith(REPAIRABLE_PREFLIGHT_ERROR_PREFIXES) for error in errors
    )
