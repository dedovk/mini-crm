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

    kind: Literal["unit_cost", "prepayment", "text"]
    unit_cost: Decimal | None = None
    text_value: str | None = None
    currency: Literal["UAH", "USD"] = "UAH"

    def __post_init__(self) -> None:
        if self.kind == "unit_cost":
            if (
                self.unit_cost is None
                or not self.unit_cost.is_finite()
                or self.unit_cost < 0
                or self.text_value is not None
                or self.currency not in {"UAH", "USD"}
            ):
                raise ValueError("unit cost record requires one non-negative finite value")
            return
        if self.kind == "prepayment":
            if (
                self.unit_cost is not None
                or self.text_value is not None
                or self.currency != "UAH"
            ):
                raise ValueError("prepayment record must not carry a value")
            return
        if self.kind == "text":
            if (
                self.unit_cost is not None
                or not (self.text_value or "").strip()
                or self.currency != "UAH"
            ):
                raise ValueError("text record requires one non-empty text value")
            return
        raise ValueError(f"unsupported supplier cost kind: {self.kind!r}")

    @classmethod
    def cost(
        cls, value: Decimal, *, currency: Literal["UAH", "USD"] = "UAH"
    ) -> SupplierCostRecord:
        return cls(kind="unit_cost", unit_cost=value, currency=currency)

    @classmethod
    def prepayment(cls) -> SupplierCostRecord:
        return cls(kind="prepayment")

    @classmethod
    def text(cls, value: str) -> SupplierCostRecord:
        """Preserve a non-empty supplier marker after trimming outer whitespace."""
        normalized = value.strip()
        if not normalized:
            raise ValueError("supplier text value must not be empty")
        return cls(kind="text", text_value=normalized)


@dataclass(frozen=True, slots=True)
class SupplierCostKey:
    """A supplier lookup key scoped to one shipment and optional product code."""

    tracking_number: str
    product_code: str = ""


@dataclass(frozen=True, slots=True)
class SupplierCostBatch:
    """Immutable normalized supplier values with their source identity."""

    source: str
    values: Mapping[SupplierCostKey, SupplierCostRecord] = field(default_factory=dict)
    sender: str = ""
    warnings: tuple[str, ...] = ()
    degraded: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))


@dataclass(frozen=True, slots=True)
class ResolvedSupplierCost:
    source: str
    record: SupplierCostRecord
    sender: str = ""


@dataclass(frozen=True, slots=True)
class SupplierCostUpdateResult:
    cell_updates: int = 0
    audit_events: tuple[OrderAuditEvent, ...] = ()
    warnings: tuple[str, ...] = ()
