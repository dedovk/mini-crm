from __future__ import annotations

import logging
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

import gspread
from google.auth.exceptions import TransportError
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError
from requests import ConnectionError, Timeout

from crm_sync.models import SupplierCostBatch, SupplierCostKey, SupplierCostRecord
from crm_sync.supplier_identity import MELAD_SENDER, MELAD_SUPPLIER_SOURCE
from crm_sync.utils import product_code_match_key, tracking_match_key

LOGGER = logging.getLogger(__name__)
_PRODUCT_CODE_RE = re.compile(r"\(([^()]*)\)")
_NUMBER_RE = re.compile(r"^[+-]?\d[\d\s\u00a0]*(?:[.,]\d+)?$")
_MAX_WARNINGS = 50
_TRACKING_COLUMN = 0
_PRODUCT_COLUMN = 6
_QUANTITY_COLUMN = 7
_UNIT_USD_COLUMN = 8
_TOTAL_USD_COLUMN = 9
_PREFIXLESS_PRM_DIGITS = 9


class MeladSupplierSheetClient:
    """Read per-item USD costs from the Melad supplier worksheet."""

    source = MELAD_SUPPLIER_SOURCE
    sender = MELAD_SENDER

    def __init__(
        self,
        *,
        credentials_info: dict[str, Any],
        spreadsheet_id: str,
        worksheet_name: str = "ВАРЧЕНКО",
        timeout: int = 60,
        max_retries: int = 4,
    ) -> None:
        self._credentials_info = credentials_info
        self._spreadsheet_id = spreadsheet_id
        self._worksheet_name = worksheet_name
        self._timeout = timeout
        self._max_retries = max_retries

    def fetch_costs(self) -> SupplierCostBatch:
        """Return non-conflicting USD unit costs keyed by TTN and product code."""
        rows = self._fetch_rows_with_retry()
        values: dict[SupplierCostKey, SupplierCostRecord] = {}
        conflicted: set[SupplierCostKey] = set()
        warnings: list[str] = []
        omitted = 0
        tracking_rows = 0
        item_rows_by_tracking: dict[str, int] = {}
        usable_rows_by_tracking: dict[
            str, list[tuple[SupplierCostKey, SupplierCostRecord]]
        ] = {}

        def warn(message: str) -> None:
            nonlocal omitted
            if len(warnings) < _MAX_WARNINGS:
                warnings.append(message)
            else:
                omitted += 1

        for row_number, row in enumerate(rows, start=1):
            tracking = _supplier_tracking_key(
                row[_TRACKING_COLUMN] if len(row) > _TRACKING_COLUMN else ""
            )
            if not tracking:
                continue
            tracking_rows += 1
            item_rows_by_tracking[tracking] = item_rows_by_tracking.get(tracking, 0) + 1
            product_name = row[_PRODUCT_COLUMN] if len(row) > _PRODUCT_COLUMN else ""
            product_code = _extract_product_code(product_name)
            quantity = _positive_decimal(
                row[_QUANTITY_COLUMN] if len(row) > _QUANTITY_COLUMN else "",
                allow_suffix=True,
            )
            unit_cost = _positive_decimal(
                row[_UNIT_USD_COLUMN] if len(row) > _UNIT_USD_COLUMN else ""
            )
            total_cost = _positive_decimal(
                row[_TOTAL_USD_COLUMN] if len(row) > _TOTAL_USD_COLUMN else ""
            )
            if quantity is None or unit_cost is None:
                warn(
                    f"Melad row {row_number}, TTN {tracking}: missing positive quantity or USD unit cost"
                )
                continue
            if total_cost is not None and abs(unit_cost * quantity - total_cost) > Decimal("0.02"):
                warn(
                    f"Melad row {row_number}, TTN {tracking}: USD total does not match unit cost × quantity"
                )
                continue
            key = SupplierCostKey(tracking, product_code)
            record = SupplierCostRecord.cost(unit_cost, currency="USD")
            usable_rows_by_tracking.setdefault(tracking, []).append((key, record))
            if key in conflicted:
                continue
            previous = values.get(key)
            if previous is not None and previous != record:
                values.pop(key, None)
                conflicted.add(key)
                warn(
                    f"Melad TTN {tracking}, product {product_code or '*'} has conflicting USD costs"
                )
                continue
            values[key] = record

        # A product-less key is also a fallback, so never retain one for a
        # physically multi-row supplier shipment, even if other rows are invalid.
        for tracking, item_count in item_rows_by_tracking.items():
            if item_count != 1:
                values.pop(SupplierCostKey(tracking), None)

        # Marketplace and supplier SKUs are not always the same. A TTN-only alias
        # is safe only when the supplier has exactly one item row for that TTN;
        # invalid sibling rows must not make a multi-item shipment look singular.
        for tracking, entries in usable_rows_by_tracking.items():
            if item_rows_by_tracking.get(tracking) != 1 or len(entries) != 1:
                continue
            key, record = entries[0]
            if key in conflicted:
                continue
            values.setdefault(SupplierCostKey(tracking), record)

        if omitted:
            warnings.append(f"Melad: {omitted} additional warning(s) omitted")
        degraded = not rows or (tracking_rows > 0 and not values)
        if not rows:
            warnings.append("Melad supplier sheet returned no rows")
        elif degraded:
            warnings.append(
                "Melad schema check failed: TTNs were found in A, but H/I had no usable costs"
            )
        usable_row_count = sum(len(entries) for entries in usable_rows_by_tracking.values())
        LOGGER.info(
            "Melad supplier sheet returned %s usable row(s), %s lookup key(s), and %s warning(s)",
            usable_row_count,
            len(values),
            len(warnings),
        )
        return SupplierCostBatch(
            source=self.source,
            values=values,
            sender=self.sender,
            warnings=tuple(warnings),
            degraded=degraded,
        )

    def _open_worksheet(self) -> Any:
        credentials = Credentials.from_service_account_info(
            self._credentials_info,
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        client = gspread.authorize(credentials)
        client.http_client.timeout = (10, max(self._timeout, 10))
        return client.open_by_key(self._spreadsheet_id).worksheet(self._worksheet_name)

    def _fetch_rows_with_retry(self) -> list[list[Any]]:
        for attempt in range(self._max_retries + 1):
            try:
                return self._open_worksheet().get("A:J", value_render_option="UNFORMATTED_VALUE")
            except (ConnectionError, Timeout, TransportError) as exc:
                retryable = True
                last_error: Exception = exc
            except APIError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", 0)
                retryable = status in {408, 429} or status >= 500
                last_error = exc
            if not retryable or attempt >= self._max_retries:
                raise last_error
            time.sleep(min(0.5 * (2**attempt), 4.0))
        raise RuntimeError("unreachable supplier retry state")


def _extract_product_code(value: Any) -> str:
    """Extract the last SKU-like parenthesized token from a product title."""
    candidates = _PRODUCT_CODE_RE.findall(str(value or ""))
    for candidate in reversed(candidates):
        stripped = " ".join(candidate.split())
        if _is_product_code_candidate(stripped):
            return product_code_match_key(stripped)
    return ""


def _is_product_code_candidate(value: str) -> bool:
    """Return whether one parenthesized Melad token looks like an SKU."""
    if not value or len(value) > 50:
        return False
    tokens = value.split()
    if len(tokens) == 1:
        return any(character.isdigit() for character in value) or any(
            separator in value for separator in "-_/"
        )
    if len(tokens) != 2:
        return False
    prefix, numeric_suffix = tokens
    normalized_prefix = prefix.upper()
    return bool(
        re.fullmatch(r"[A-Z][A-Z0-9_./-]*", normalized_prefix)
        and re.fullmatch(r"\d[A-Z0-9_./-]*", numeric_suffix.upper())
    )


def _supplier_tracking_key(value: Any) -> str:
    """Normalize TTNs; Melad's column omits ``PRM-`` from bare Prom identifiers."""
    raw = str(value or "").strip()
    known_tracking = tracking_match_key(raw)
    if known_tracking:
        return known_tracking
    compact_digits = re.sub(r"[\s\u00a0]+", "", raw)
    if re.fullmatch(rf"\d{{{_PREFIXLESS_PRM_DIGITS}}}", compact_digits):
        return tracking_match_key(f"PRM-{compact_digits}")
    return ""


def _positive_decimal(value: Any, *, allow_suffix: bool = False) -> Decimal | None:
    raw = str(value or "").strip()
    if allow_suffix:
        match = re.match(r"^[+-]?\d[\d\s\u00a0]*(?:[.,]\d+)?", raw)
        raw = match.group(0) if match else ""
    if not _NUMBER_RE.fullmatch(raw):
        return None
    try:
        parsed = Decimal(raw.replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None
