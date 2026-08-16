from datetime import UTC, datetime
from decimal import Decimal

from crm_sync.integrity import validate_incoming_orders
from crm_sync.models import Order, OrderItem


def make_order(*, quantity: Decimal = Decimal(1), line_total: Decimal = Decimal(100)) -> Order:
    return Order(
        source="prom",
        external_id="501",
        created_at=datetime(2026, 8, 5, tzinfo=UTC),
        completed_at=datetime(2026, 8, 5, tzinfo=UTC),
        customer_name="Customer",
        city="Kyiv",
        phone="+380501234567",
        tracking_number="20451234567890",
        total=Decimal(100),
        payment_method="наложка",
        note="",
        sender="наш",
        items=[
            OrderItem(
                name="Product",
                product_code="SKU",
                quantity=quantity,
                unit_price=Decimal(100),
                line_total=line_total,
            )
        ],
    )


def test_incoming_integrity_warns_about_duplicates_and_rejects_invalid_quantity() -> None:
    first = make_order(quantity=Decimal(0))
    report = validate_incoming_orders([first, make_order()])

    assert not report.ok
    assert any("duplicate order" in warning for warning in report.warnings)
    assert any("quantity must be positive" in error for error in report.errors)


def test_incoming_integrity_warns_about_inconsistent_line_total() -> None:
    report = validate_incoming_orders([make_order(line_total=Decimal(90))])

    assert report.ok
    assert "price × quantity" in report.warnings[0]


def test_incoming_integrity_rejects_invalid_financial_fields_and_identity() -> None:
    order = make_order()
    order.source = ""
    order.external_id = ""
    order.prepayment = Decimal(101)
    order.advertising_cost = Decimal(-1)
    order.installment_commission = Decimal(-2)

    report = validate_incoming_orders([order])

    assert not report.ok
    assert any("source is empty" in error for error in report.errors)
    assert any("external order ID is empty" in error for error in report.errors)
    assert any("prepayment exceeds" in error for error in report.errors)
    assert any("advertising cost is negative" in error for error in report.errors)
    assert any("installment commission is negative" in error for error in report.errors)
