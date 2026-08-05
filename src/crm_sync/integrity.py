from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from crm_sync.models import Order


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


class IntegrityError(RuntimeError):
    def __init__(self, report: IntegrityReport) -> None:
        self.report = report
        super().__init__("; ".join(report.errors))


def validate_incoming_orders(orders: list[Order]) -> IntegrityReport:
    errors: list[str] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for order in orders:
        key = order.sync_key.casefold()
        if key in seen:
            errors.append(f"API returned duplicate order {order.sync_key}")
            continue
        seen.add(key)
        if not order.items:
            errors.append(f"{order.sync_key}: order has no product rows")
            continue
        if order.total < Decimal(0):
            errors.append(f"{order.sync_key}: order total is negative")
        if not order.tracking_number.strip():
            errors.append(f"{order.sync_key}: tracking number is empty")
        for index, item in enumerate(order.items, start=1):
            prefix = f"{order.sync_key} item {index}"
            if item.quantity <= Decimal(0):
                errors.append(f"{prefix}: quantity must be positive")
            if item.unit_price < Decimal(0) or item.line_total < Decimal(0):
                errors.append(f"{prefix}: monetary values must not be negative")
            expected = item.unit_price * item.quantity
            if abs(expected - item.line_total) > Decimal("0.02"):
                warnings.append(
                    f"{prefix}: line total {item.line_total} differs from price × quantity {expected}"
                )
    return IntegrityReport(tuple(errors), tuple(warnings))
