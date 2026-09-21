from decimal import Decimal

import pytest

from crm_sync.installment_commissions import (
    InstallmentCommissionState,
    resolve_installment_commission,
)


@pytest.mark.parametrize("estimate_source", ["fallback", "tariff", "unresolved"])
def test_unresolved_prom_value_removes_estimates(estimate_source: str) -> None:
    resolved = resolve_installment_commission(
        InstallmentCommissionState(Decimal("216.41"), estimate_source),  # type: ignore[arg-type]
        InstallmentCommissionState(Decimal(0), "unresolved"),
    )

    assert resolved == InstallmentCommissionState(Decimal(0), "unresolved")


def test_reported_prom_value_replaces_estimate() -> None:
    resolved = resolve_installment_commission(
        InstallmentCommissionState(Decimal("216.41"), "fallback"),
        InstallmentCommissionState(Decimal("99.43"), "reported"),
    )

    assert resolved == InstallmentCommissionState(Decimal("99.43"), "reported")


@pytest.mark.parametrize("source", ["reported", "legacy", ""])
def test_unresolved_payload_preserves_non_estimated_positive_value(source: str) -> None:
    resolved = resolve_installment_commission(
        InstallmentCommissionState(Decimal("99.43"), source),  # type: ignore[arg-type]
        InstallmentCommissionState(Decimal(0), "unresolved"),
    )

    expected_source = source or "legacy"
    assert resolved == InstallmentCommissionState(
        Decimal("99.43"), expected_source  # type: ignore[arg-type]
    )
