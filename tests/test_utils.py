from decimal import Decimal

import pytest

from crm_sync.utils import (
    classify_payment,
    customer_display,
    extract_ttn,
    find_tracking_number,
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
    assert extract_ttn("ЕН 20 4515 0157 2223") == "20451501572223"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ТТН: 20 4515 0157 2223", "20 4515 0157 2223"),
        ("RMP-483122083", "RMP-483122083"),
        ("RR123456789UA", "RR123456789UA"),
        ("MEEST-123-456789", "MEEST-123-456789"),
        ("123-456789012", "123-456789012"),
        ("ABC-456789012", "ABC-456789012"),
    ],
)
def test_find_tracking_number_supports_multiple_carriers(raw: str, expected: str) -> None:
    assert find_tracking_number(raw) == expected


def test_tracking_number_rejects_internal_hyphenated_identifier() -> None:
    assert find_tracking_number("909-46-65") == ""


def test_customer_display_keeps_city_surname_and_first_name() -> None:
    assert customer_display("Самар", "Зенькович Олександр Михайлович") == "Самар, Зенькович Олександр"
