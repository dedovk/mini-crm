from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Literal, Mapping

InstallmentCommissionSource = Literal["", "reported", "tariff", "fallback", "legacy"]


@dataclass(slots=True)
class OrderItem:
    name: str
    product_code: str
    quantity: Decimal
    unit_price: Decimal
    line_total: Decimal


@dataclass(slots=True)
class Order:
    source: str
    external_id: str
    created_at: datetime
    completed_at: datetime
    customer_name: str
    city: str
    phone: str
    tracking_number: str
    total: Decimal
    payment_method: str
    note: str
    sender: str
    channel: str = ""
    completion_is_exact: bool = True
    source_status: str = "Виконано"
    items: list[OrderItem] = field(default_factory=list)
    prepayment: Decimal = Decimal(0)
    advertising_cost: Decimal = Decimal(0)
    installment_commission: Decimal = Decimal(0)
    installment_commission_source: InstallmentCommissionSource = ""
    updated_at: datetime | None = None

    @property
    def sync_key(self) -> str:
        return f"{self.source.casefold()}:{self.external_id.strip()}"

    @property
    def is_completed(self) -> bool:
        return self.source_status.strip().casefold() == "виконано"

    @property
    def is_cancelled(self) -> bool:
        status = self.source_status.strip().casefold()
        return status in {"скасовано", "отменено", "canceled", "cancelled"}


@dataclass(slots=True)
class ShipmentStatus:
    tracking_number: str
    status: str
    status_code: str = ""
    redelivery_sum: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class ShipmentStatusChange:
    source: str
    order_id: str
    sync_key: str
    tracking_number: str
    old_status: str
    new_status: str


@dataclass(frozen=True, slots=True)
class ShipmentUpdateResult:
    cell_updates: int = 0
    changes: tuple[ShipmentStatusChange, ...] = ()


@dataclass(frozen=True, slots=True)
class OrderAuditEvent:
    occurred_at: datetime
    event_type: str
    source: str
    order_id: str
    sync_key: str
    tracking_number: str
    field: str = ""
    old_value: str = ""
    new_value: str = ""
    details: str = ""


@dataclass(frozen=True, slots=True)
class OrderExpenseTransaction:
    transaction_id: str
    order_id: str
    category: Literal["royalty", "logistics_charge", "logistics_refund"]
    debit: Decimal = Decimal(0)
    credit: Decimal = Decimal(0)

    @property
    def expense_effect(self) -> Decimal:
        if self.category == "logistics_refund":
            return -abs(self.credit or self.debit)
        return abs(self.debit) - abs(self.credit)


@dataclass(frozen=True, slots=True)
class SyncHealthState:
    consecutive_failures: int = 0
    alert_due: bool = False
    recovered: bool = False
    failed_components: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SupplierCostRecord:
    """A typed supplier value independent from its sheet presentation."""

    kind: Literal["unit_cost", "prepayment"]
    unit_cost: Decimal | None = None

    @classmethod
    def cost(cls, value: Decimal) -> SupplierCostRecord:
        return cls(kind="unit_cost", unit_cost=value)

    @classmethod
    def prepayment(cls) -> SupplierCostRecord:
        return cls(kind="prepayment")


@dataclass(frozen=True, slots=True)
class SupplierCostBatch:
    """Immutable normalized supplier values with their source identity."""

    source: str
    values: Mapping[str, SupplierCostRecord] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    degraded: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True, slots=True)
class ResolvedSupplierCost:
    source: str
    record: SupplierCostRecord


@dataclass(frozen=True, slots=True)
class SupplierCostUpdateResult:
    cell_updates: int = 0
    audit_events: tuple[OrderAuditEvent, ...] = ()
