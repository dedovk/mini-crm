from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal


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
    completion_is_exact: bool = True
    items: list[OrderItem] = field(default_factory=list)
    advertising_cost: Decimal = Decimal(0)

    @property
    def sync_key(self) -> str:
        return f"{self.source.casefold()}:{self.external_id.strip()}"


@dataclass(slots=True)
class ShipmentStatus:
    tracking_number: str
    status: str
    status_code: str = ""


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
