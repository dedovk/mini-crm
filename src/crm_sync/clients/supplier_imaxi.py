from __future__ import annotations

import logging
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError
from requests import ConnectionError, Timeout

from crm_sync.models import SupplierCostBatch, SupplierCostRecord
from crm_sync.utils import tracking_match_key

LOGGER = logging.getLogger(__name__)
_COST_COLUMN_OFFSET = 6  # R is the seventh column in the requested L:R range.
_PREPAYMENT = "предоплата"
_NUMBER_RE = re.compile(r"^[+-]?\d[\d\s\u00a0]*(?:[.,]\d{1,2})?$")
_MAX_DETAILED_WARNINGS = 50
_MAX_CELL_TEXT_LENGTH = 50_000


class ImaxiSupplierSheetClient:
    """Read IMAXI unit costs from one supplier-owned Google worksheet."""

    source = "supplier-imaxi"

    def __init__(
        self,
        *,
        credentials_info: dict[str, Any],
        spreadsheet_id: str,
        worksheet_name: str = "Лист1",
        timeout: int = 60,
        max_retries: int = 4,
    ) -> None:
        self._credentials_info = credentials_info
        self._spreadsheet_id = spreadsheet_id
        self._worksheet_name = worksheet_name
        self._timeout = timeout
        self._max_retries = max_retries

    def fetch_costs(self) -> SupplierCostBatch:
        """Fetch L:R once and return non-conflicting values indexed by TTN."""
        rows = self._fetch_rows_with_retry()
        values: dict[str, SupplierCostRecord] = {}
        conflicted: set[str] = set()
        warnings: list[str] = []
        omitted_warnings = 0
        tracking_rows = 0

        def warn(message: str) -> None:
            nonlocal omitted_warnings
            if len(warnings) < _MAX_DETAILED_WARNINGS:
                warnings.append(message)
            else:
                omitted_warnings += 1

        for row_number, row in enumerate(rows, start=1):
            tracking = _tracking_key(row[0] if row else "")
            raw_cost = row[_COST_COLUMN_OFFSET] if len(row) > _COST_COLUMN_OFFSET else ""
            if not tracking:
                continue
            tracking_rows += 1
            if str(raw_cost).strip() == "":
                continue
            cost, error = _parse_cost(raw_cost)
            if error:
                warn(f"IMAXI row {row_number}, TTN {tracking}: {error}")
                continue
            if cost is None or tracking in conflicted:
                continue
            previous = values.get(tracking)
            if previous is not None and previous != cost:
                values.pop(tracking, None)
                conflicted.add(tracking)
                warn(
                    f"IMAXI TTN {tracking} has conflicting costs; it was not imported"
                )
                continue
            values[tracking] = cost

        if omitted_warnings:
            warnings.append(f"IMAXI: {omitted_warnings} additional warning(s) omitted")
        degraded = tracking_rows > 0 and not values
        if degraded:
            warnings.append(
                "IMAXI schema check failed: TTNs were found in L, but R had no usable costs"
            )
        LOGGER.info(
            "IMAXI supplier sheet returned %s usable TTN cost(s) and %s warning(s)",
            len(values),
            len(warnings),
        )
        return SupplierCostBatch(
            source=self.source,
            values=values,
            warnings=tuple(warnings),
            degraded=degraded,
        )

    def _open_worksheet(self) -> Any:
        """Open the optional worksheet lazily inside the service failure boundary."""
        credentials = Credentials.from_service_account_info(
            self._credentials_info,
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        client = gspread.authorize(credentials)
        client.http_client.timeout = (10, max(self._timeout, 10))
        spreadsheet = client.open_by_key(self._spreadsheet_id)
        return spreadsheet.worksheet(self._worksheet_name)

    def _fetch_rows_with_retry(self) -> list[list[Any]]:
        for attempt in range(self._max_retries + 1):
            try:
                worksheet = self._open_worksheet()
                return worksheet.get("L:R", value_render_option="UNFORMATTED_VALUE")
            except (ConnectionError, Timeout) as exc:
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


def _tracking_key(value: Any) -> str:
    return tracking_match_key(value)


def _parse_cost(value: Any) -> tuple[SupplierCostRecord | None, str]:
    raw = str(value).strip()
    if len(raw) > _MAX_CELL_TEXT_LENGTH:
        return None, "text cost exceeds the Google Sheets 50000-character cell limit"
    if raw.casefold() == _PREPAYMENT:
        return SupplierCostRecord.prepayment(), ""
    if not _NUMBER_RE.fullmatch(raw):
        return SupplierCostRecord.text(raw), ""
    normalized = raw.replace("\u00a0", "").replace(" ", "").replace(",", ".")
    try:
        cost = Decimal(normalized)
    except InvalidOperation:
        return SupplierCostRecord.text(raw), ""
    if not cost.is_finite() or cost < 0:
        return SupplierCostRecord.text(raw), ""
    return SupplierCostRecord.cost(cost), ""
