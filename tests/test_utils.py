from decimal import Decimal

import pytest

from crm_sync.utils import (
    classify_payment,
    customer_display,
    decimal_value,
    extract_ttn,
    find_tracking_number,
    normalize_phone,
    normalize_shipment_status,
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
        ("Предоплата 200 грн.", Decimal(200)),
        ("пред 300", Decimal(300)),
        ("предо - 450", Decimal(450)),
        ("без передоплати", Decimal(0)),
        ("без предоплати 500", Decimal(0)),
    ],
)
def test_parse_prepayment(note: str, expected: Decimal) -> None:
    assert parse_prepayment(note) == expected


def test_mixed_payment_from_prepayment_and_cod() -> None:
    assert classify_payment("Наложенный платеж", "перед - 300") == "смешанная"


def test_prepayment_marker_without_amount_still_marks_mixed_payment() -> None:
    assert classify_payment("Наложенный платеж", "клієнту запитали пред") == "смешанная"


@pytest.mark.parametrize(
    "status",
    [
        "Відправлення у дорозі",
        "Прибуло у відділення",
        "Передано кур'єру",
        "Прийнято у відділенні",
    ],
)
def test_normalize_shipment_status_groups_transit_states(status: str) -> None:
    assert normalize_shipment_status(status) == "Прямує до покупця"


def test_normalize_shipment_status_preserves_final_state() -> None:
    assert normalize_shipment_status("Отримано") == "Отримано"


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


def test_decimal_value_parses_prom_currency_and_nested_amount() -> None:
    assert decimal_value("1\u00a0149 грн") == Decimal(1149)
    assert decimal_value({"amount": "69.30"}) == Decimal("69.30")
