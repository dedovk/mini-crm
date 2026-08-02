from decimal import Decimal

import pytest

from crm_sync.utils import (
    classify_payment,
    extract_ttn,
    normalize_phone,
    parse_prepayment,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0501234567", "+380501234567"),
        ("380501234567", "+380501234567"),
        ("+38 (050) 123-45-67", "+380501234567"),
    ],
)
def test_normalize_phone(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    ("note", "expected"),
    [
        ("перед - 500", Decimal(500)),
        ("Предоплата: 1 250,50", Decimal("1250.50")),
        ("без передоплати", Decimal(0)),
    ],
)
def test_parse_prepayment(note: str, expected: Decimal) -> None:
    assert parse_prepayment(note) == expected


def test_mixed_payment_from_prepayment_and_cod() -> None:
    assert classify_payment("Наложенный платеж", "перед - 300") == "смешанная"


def test_extract_ttn() -> None:
    assert extract_ttn("ТТН 20451234567890 створена") == "20451234567890"

