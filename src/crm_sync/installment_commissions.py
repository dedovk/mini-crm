from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from crm_sync.models import InstallmentCommissionSource

_ESTIMATED_SOURCES = frozenset({"fallback", "tariff", "unresolved"})


@dataclass(frozen=True, slots=True)
class InstallmentCommissionState:
    """A commission amount together with the authority that supplied it."""

    amount: Decimal = Decimal(0)
    source: InstallmentCommissionSource = ""


def resolve_installment_commission(
    current: InstallmentCommissionState,
    incoming: InstallmentCommissionState,
) -> InstallmentCommissionState:
    """Resolve commission precedence without inventing a financial value.

    A fee explicitly reported by Prom is authoritative.  Missing Prom data
    invalidates old tariff/fallback estimates, but does not destroy a positive
    value previously marked as reported or a legacy/manual value whose origin
    cannot safely be disproved.
    """
    if incoming.source == "reported":
        return incoming
    if incoming.source == "unresolved":
        if current.amount > 0 and current.source not in _ESTIMATED_SOURCES:
            return InstallmentCommissionState(
                current.amount,
                current.source or "legacy",
            )
        return InstallmentCommissionState(Decimal(0), "unresolved")
    if current.amount > 0:
        return InstallmentCommissionState(
            current.amount,
            current.source or "legacy",
        )
    return incoming
