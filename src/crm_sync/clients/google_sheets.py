from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping

import gspread
from google.oauth2.service_account import Credentials
from gspread import BackOffHTTPClient
from gspread.utils import rowcol_to_a1

from crm_sync.integrity import IntegrityReport
from crm_sync.models import (
    Order,
    OrderAuditEvent,
    ResolvedSupplierCost,
    ShipmentStatus,
    ShipmentStatusChange,
    ShipmentUpdateResult,
    SupplierCostKey,
    SupplierCostRecord,
    SupplierCostUpdateResult,
    SyncHealthState,
)
from crm_sync.sheet_layout import (
    ALL_HEADERS,
    BUSINESS_HEADERS,
    REPORTING_EXCLUDED_REFUSAL,
    ROW_DAY,
    ROW_HEADER,
    ROW_MONTH,
    ROW_ORDER,
    ROW_REPORT_DAY,
    ROW_REPORT_FORECAST,
    ROW_REPORT_MTD,
    TECHNICAL_HEADERS,
    parse_sheet_date,
    sheet_serial,
    source_display,
    source_key,
)
from crm_sync.sheet_meta import (
    append_sheet_audit_events,
    create_sheet_backup,
    ensure_sheet_audit_worksheet,
    record_sheet_sync_health,
)
from crm_sync.sheet_orders import (
    advertising_display,
    collect_order_groups,
    markup_formula,
    net_profit_formula,
    row_has_prepayment,
    row_is_refused,
)
from crm_sync.sheet_schema import (
    COLUMNS,
    LAST_COLUMN,
    LAST_COLUMN_LETTER,
    NOVA_POSHTA_STATUS_OPTIONS,
    PAYMENT_OPTIONS,
)
from crm_sync.sheet_snapshot import (
    DAY_DATE_LABEL,
    USD_RATE_LABEL,
    USD_RATE_LABEL_COLUMN,
    USD_RATE_VALUE_COLUMN,
    build_sheet_snapshot,
)
from crm_sync.supplier_identity import MELAD_SENDER, MELAD_SUPPLIER_SOURCE
from crm_sync.utils import (
    customer_display,
    decimal_for_sheet,
    decimal_value,
    extract_ttn,
    normalize_tracking_number,
    parse_prepayment,
    product_code_match_key,
    tracking_match_key,
)

LOGGER = logging.getLogger(__name__)
LEGACY_INSTALLMENT_HEADER = "Комісія оплати частинами, грн"
SUPPLIER_SENDER_DEFAULTS = {
    "supplier-imaxi": "imaxi-com",
    MELAD_SUPPLIER_SOURCE: MELAD_SENDER,
}
_USD_RATE_PATTERN = re.compile(r"^\d{2,3}(?:[.,]\d{1,4})?$")
_MIN_USD_RATE = Decimal("20")
_MAX_USD_RATE = Decimal("100")


class ConcurrentSheetEditError(RuntimeError):
    """The sheet changed while a structural rebuild was being prepared."""


class SheetSchemaMigrationError(RuntimeError):
    """The existing sheet layout cannot be migrated without risking data loss."""


@dataclass(frozen=True, slots=True)
class _SupplierCostCandidate:
    row_number: int
    original_row: list[Any]
    key: SupplierCostKey
    expected: ResolvedSupplierCost
    operational_day: date | None
    rate: Decimal | None


class GoogleSheetsGateway:
    def __init__(
        self,
        *,
        credentials_info: dict[str, Any],
        spreadsheet_id: str,
        worksheet_name: str,
        header_row: int,
        sender_options: tuple[str, ...],
        timeout: int = 60,
    ) -> None:
        credentials = Credentials.from_service_account_info(
            credentials_info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        client = gspread.authorize(credentials, http_client=BackOffHTTPClient)
        client.http_client.timeout = (10, max(timeout, 10))
        self.spreadsheet = client.open_by_key(spreadsheet_id)
        self.worksheet = self.spreadsheet.worksheet(worksheet_name)
        self.header_row = header_row
        self.sender_options = sender_options

    def append_audit_events(self, events: list[OrderAuditEvent]) -> int:
        return append_sheet_audit_events(self.spreadsheet, events)

    def create_backup(self, *, created_at: datetime) -> str:
        return create_sheet_backup(self.spreadsheet, self.worksheet, created_at=created_at)

    def record_sync_health(
        self,
        failed_components: list[str],
        *,
        occurred_at: datetime,
    ) -> SyncHealthState:
        return record_sheet_sync_health(
            self.spreadsheet,
            failed_components,
            occurred_at=occurred_at,
        )

    def ensure_schema(self, *, apply_changes: bool = True) -> None:
        self._locate_header_row()
        headers = self.worksheet.row_values(self.header_row)
        required_signals = {
            COLUMNS.source: ("джерело",),
            COLUMNS.tracking_number: ("ттн",),
            COLUMNS.order_number: ("номер замовлення", "№ замовлення"),
            COLUMNS.product: ("товар",),
            COLUMNS.order_total: ("загальна сума замовлення", "сума замовлення"),
        }
        for column, signals in required_signals.items():
            value = headers[column - 1].casefold() if len(headers) >= column else ""
            if not any(signal in value for signal in signals):
                raise RuntimeError(
                    f"Google Sheets schema mismatch at {rowcol_to_a1(self.header_row, column)}: "
                    f"expected header containing one of {signals!r}"
                )

        if apply_changes:
            if self._migrate_net_profit_column(headers):
                headers = self.worksheet.row_values(self.header_row)
            self._migrate_legacy_installment_column(headers)
            if self.worksheet.col_count < LAST_COLUMN:
                self.worksheet.add_cols(LAST_COLUMN - self.worksheet.col_count)
            self.worksheet.update(
                values=[list(ALL_HEADERS[COLUMNS.sync_key - 1 :])],
                range_name=(
                    f"{rowcol_to_a1(self.header_row, COLUMNS.sync_key)}:"
                    f"{rowcol_to_a1(self.header_row, LAST_COLUMN)}"
                ),
                raw=True,
            )
            self._configure_validations_and_hidden_key()
            ensure_sheet_audit_worksheet(self.spreadsheet)
            self._refresh_end_navigation_link()

    def _migrate_net_profit_column(self, headers: list[str]) -> bool:
        """Insert the new business column without overwriting notes or technical data."""
        current = (
            str(headers[COLUMNS.net_profit - 1]).strip().casefold()
            if len(headers) >= COLUMNS.net_profit
            else ""
        )
        expected = BUSINESS_HEADERS[COLUMNS.net_profit - 1].casefold()
        if current == expected:
            return False
        legacy_sync_key = (
            str(headers[COLUMNS.sync_key - 2]).strip().casefold()
            if len(headers) >= COLUMNS.sync_key - 1
            else ""
        )
        legacy_row_type = (
            str(headers[COLUMNS.row_type - 2]).strip().casefold()
            if len(headers) >= COLUMNS.row_type - 1
            else ""
        )
        legacy_layout = (
            legacy_sync_key == TECHNICAL_HEADERS[0].casefold()
            and legacy_row_type == TECHNICAL_HEADERS[1].casefold()
        )
        if not any(str(value).strip() for value in headers):
            return False
        if not legacy_layout:
            raise SheetSchemaMigrationError(
                "Cannot safely insert the net-profit column: existing technical "
                "headers do not match either the current or legacy layout"
            )
        self.spreadsheet.batch_update(
            {
                "requests": [
                    {
                        "insertDimension": {
                            "range": {
                                "sheetId": self.worksheet.id,
                                "dimension": "COLUMNS",
                                "startIndex": COLUMNS.net_profit - 1,
                                "endIndex": COLUMNS.net_profit,
                            },
                            "inheritFromBefore": True,
                        }
                    }
                ]
            }
        )
        return True

    def _locate_header_row(self) -> None:
        """Locate the first repeated business header in the active sheet."""
        preview = self.worksheet.get(
            f"A1:B{min(self.worksheet.row_count, 100)}",
            value_render_option="UNFORMATTED_VALUE",
        )
        located_header = next(
            (
                row_number
                for row_number, row in enumerate(preview, start=1)
                if len(row) >= 2
                and str(row[0]).strip().casefold() == BUSINESS_HEADERS[0].casefold()
                and str(row[1]).strip().casefold() == BUSINESS_HEADERS[1].casefold()
            ),
            self.header_row,
        )
        self.header_row = located_header

    def _migrate_legacy_installment_column(self, headers: list[str]) -> None:
        """Move legacy AA installment values to AB before AA becomes the receipt column."""
        legacy_header = (
            str(headers[COLUMNS.receipt - 1]).strip().casefold()
            if len(headers) >= COLUMNS.receipt
            else ""
        )
        expected = LEGACY_INSTALLMENT_HEADER.casefold()
        if legacy_header != expected:
            return
        sheet_id = self.worksheet.id
        self.spreadsheet.batch_update(
            {
                "requests": [
                    {
                        "copyPaste": {
                            "source": {
                                "sheetId": sheet_id,
                                "startColumnIndex": COLUMNS.receipt - 1,
                                "endColumnIndex": COLUMNS.receipt,
                            },
                            "destination": {
                                "sheetId": sheet_id,
                                "startColumnIndex": COLUMNS.installment_commission - 1,
                                "endColumnIndex": COLUMNS.installment_commission,
                            },
                            "pasteType": "PASTE_NORMAL",
                            "pasteOrientation": "NORMAL",
                        }
                    }
                ]
            }
        )
        # Keep copy and clear as separate idempotent requests. If the HTTP client
        # retries a request after a lost response, AB cannot be overwritten from
        # an already-cleared AA column.
        self.worksheet.batch_clear(
            [
                f"{rowcol_to_a1(1, COLUMNS.receipt)}:"
                f"{rowcol_to_a1(self.worksheet.row_count, COLUMNS.receipt)}"
            ]
        )

    def _refresh_end_navigation_link(self, last_used_row: int | None = None) -> None:
        spreadsheet_id = str(getattr(self.spreadsheet, "id", "")).strip()
        if not spreadsheet_id:
            return
        if last_used_row is None:
            values = self.worksheet.get_all_values(value_render_option="FORMULA")
            last_used_row = max(1, len(values))
        url = (
            f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
            f"?gid={self.worksheet.id}#gid={self.worksheet.id}&range=A{last_used_row}"
        )
        self.spreadsheet.batch_update(
            {
                "requests": [
                    {
                        "updateCells": {
                            "range": {
                                "sheetId": self.worksheet.id,
                                "startRowIndex": 0,
                                "endRowIndex": 1,
                                "startColumnIndex": 3,
                                "endColumnIndex": 4,
                            },
                            "rows": [
                                {
                                    "values": [
                                        {
                                            "userEnteredValue": {"stringValue": "↓ До кінця"},
                                            "textFormatRuns": [
                                                {
                                                    "startIndex": 0,
                                                    "format": {
                                                        "link": {"uri": url},
                                                        "bold": True,
                                                        "fontSize": 8,
                                                        "foregroundColorStyle": {
                                                            "rgbColor": {
                                                                "red": 1,
                                                                "green": 1,
                                                                "blue": 1,
                                                            }
                                                        },
                                                    },
                                                }
                                            ],
                                        }
                                    ]
                                }
                            ],
                            "fields": "userEnteredValue,textFormatRuns",
                        }
                    }
                ]
            }
        )

    def validate_integrity(self) -> IntegrityReport:
        values = self.worksheet.get_all_values(value_render_option="FORMATTED_VALUE")
        errors: list[str] = []
        warnings: list[str] = []
        formula_errors = (
            "#ERROR!",
            "#REF!",
            "#VALUE!",
            "#DIV/0!",
            "#NAME?",
            "#N/A",
        )
        rows_by_key: dict[str, list[int]] = {}
        totals_by_key: dict[str, set[Decimal]] = {}
        missing_completion_keys: set[str] = set()
        managed_report_rows = {ROW_REPORT_DAY, ROW_REPORT_MTD, ROW_REPORT_FORECAST}

        for row_number, row in enumerate(values, start=1):
            row_type = (
                str(row[COLUMNS.row_type - 1]).strip()
                if len(row) >= COLUMNS.row_type
                else ""
            )
            for column, value in enumerate(row, start=1):
                rendered = str(value).strip()
                if rendered in formula_errors:
                    repairable = (
                        row_type == ROW_ORDER
                        and column in {COLUMNS.markup, COLUMNS.net_profit}
                    ) or row_type in managed_report_rows
                    prefix = "repairable " if repairable else ""
                    errors.append(
                        f"{prefix}formula error at "
                        f"{rowcol_to_a1(row_number, column)}: {rendered}"
                    )
            if row_type != ROW_ORDER:
                continue
            sync_key = (
                str(row[COLUMNS.sync_key - 1]).strip().casefold()
                if len(row) >= COLUMNS.sync_key
                else ""
            )
            if not sync_key:
                errors.append(f"order row {row_number} has no Sync Key")
                continue
            rows_by_key.setdefault(sync_key, []).append(row_number)
            cost = decimal_value(row[COLUMNS.cost - 1] if len(row) >= COLUMNS.cost else "")
            if cost < 0:
                errors.append(f"negative unit cost at Q{row_number}")
            raw_total = row[COLUMNS.order_total - 1] if len(row) >= COLUMNS.order_total else ""
            if str(raw_total).strip():
                totals_by_key.setdefault(sync_key, set()).add(decimal_value(raw_total))
            if not str(row[COLUMNS.order_date - 1] if len(row) >= COLUMNS.order_date else "").strip():
                missing_completion_keys.add(sync_key)

        for sync_key, row_numbers in rows_by_key.items():
            expected = list(range(min(row_numbers), max(row_numbers) + 1))
            if row_numbers != expected:
                errors.append(f"{sync_key}: product rows are split across the worksheet")
            totals = totals_by_key.get(sync_key, set())
            if len(totals) > 1:
                errors.append(f"{sync_key}: conflicting order totals {sorted(totals)}")

        if missing_completion_keys:
            warnings.append(
                f"{len(missing_completion_keys)} order(s) do not yet have a recorded completion date"
            )

        return IntegrityReport(tuple(dict.fromkeys(errors)), tuple(dict.fromkeys(warnings)))

    def _configure_validations_and_hidden_key(self) -> None:
        row_count = self.worksheet.row_count

        def validation_request(column: int, values: tuple[str, ...]) -> dict[str, Any]:
            return {
                "setDataValidation": {
                    "range": {
                        "sheetId": self.worksheet.id,
                        "startRowIndex": self.header_row,
                        "endRowIndex": row_count,
                        "startColumnIndex": column - 1,
                        "endColumnIndex": column,
                    },
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_LIST",
                            "values": [{"userEnteredValue": value} for value in values],
                        },
                        "strict": False,
                        "showCustomUi": True,
                    },
                }
            }

        requests = [
            validation_request(COLUMNS.shipment_status, NOVA_POSHTA_STATUS_OPTIONS),
            validation_request(COLUMNS.sender, self.sender_options),
            validation_request(COLUMNS.payment_method, PAYMENT_OPTIONS),
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": self.worksheet.id,
                        "dimension": "COLUMNS",
                        "startIndex": COLUMNS.sync_key - 1,
                        "endIndex": COLUMNS.receipt - 1,
                    },
                    "properties": {"hiddenByUser": True},
                    "fields": "hiddenByUser",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": self.worksheet.id,
                        "dimension": "COLUMNS",
                        "startIndex": COLUMNS.receipt - 1,
                        "endIndex": COLUMNS.receipt,
                    },
                    "properties": {"hiddenByUser": False},
                    "fields": "hiddenByUser",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": self.worksheet.id,
                        "dimension": "COLUMNS",
                        "startIndex": COLUMNS.installment_commission - 1,
                        "endIndex": LAST_COLUMN,
                    },
                    "properties": {"hiddenByUser": True},
                    "fields": "hiddenByUser",
                }
            },
        ]
        self.spreadsheet.batch_update({"requests": requests})

    def _apply_professional_formatting(self, last_used_row: int) -> None:
        values = self.worksheet.get(
            f"A1:{LAST_COLUMN_LETTER}{last_used_row}",
            value_render_option="UNFORMATTED_VALUE",
        )
        typed_rows: dict[str, list[int]] = {}
        order_groups: dict[str, list[int]] = {}
        for row_number, row in enumerate(values, start=1):
            row_type = str(row[COLUMNS.row_type - 1]).strip() if len(row) >= COLUMNS.row_type else ""
            if (
                row
                and str(row[0]).strip().casefold() == BUSINESS_HEADERS[0].casefold()
                and len(row) > 1
                and str(row[1]).strip().casefold() == BUSINESS_HEADERS[1].casefold()
            ):
                row_type = ROW_HEADER
            if row_type:
                typed_rows.setdefault(row_type, []).append(row_number)
            if row_type == ROW_ORDER and len(row) >= COLUMNS.sync_key:
                sync_key = str(row[COLUMNS.sync_key - 1]).strip().casefold()
                if sync_key:
                    order_groups.setdefault(sync_key, []).append(row_number)

        sheet_id = self.worksheet.id
        row_count = self.worksheet.row_count
        requests: list[dict[str, Any]] = [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": 4,
                    },
                    "cell": {"userEnteredFormat": {"textFormat": {"fontSize": 8}}},
                    "fields": "userEnteredFormat.textFormat.fontSize",
                }
            },
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sheet_id,
                        "gridProperties": {
                            "frozenRowCount": self.header_row,
                            "frozenColumnCount": 2,
                            "hideGridlines": True,
                        },
                    },
                    "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount,gridProperties.hideGridlines",
                }
            },
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": last_used_row,
                        "startColumnIndex": 0,
                        "endColumnIndex": LAST_COLUMN,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {"fontFamily": "Arial", "fontSize": 8},
                            "backgroundColorStyle": {"rgbColor": self._hex_color("#FFFFFF")},
                            "horizontalAlignment": "CENTER",
                            "verticalAlignment": "MIDDLE",
                            "wrapStrategy": "WRAP",
                        }
                    },
                    "fields": "userEnteredFormat(textFormat,backgroundColorStyle,horizontalAlignment,verticalAlignment,wrapStrategy)",
                }
            },
            {
                "updateBorders": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": last_used_row,
                        "startColumnIndex": 0,
                        "endColumnIndex": LAST_COLUMN,
                    },
                    "top": {"style": "NONE"},
                    "bottom": {"style": "NONE"},
                    "left": {"style": "NONE"},
                    "right": {"style": "NONE"},
                    "innerHorizontal": {"style": "NONE"},
                    "innerVertical": {"style": "NONE"},
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": COLUMNS.sync_key - 1,
                        "endIndex": COLUMNS.receipt - 1,
                    },
                    "properties": {"hiddenByUser": True},
                    "fields": "hiddenByUser",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": COLUMNS.receipt - 1,
                        "endIndex": COLUMNS.receipt,
                    },
                    "properties": {"hiddenByUser": False, "pixelSize": 115},
                    "fields": "hiddenByUser,pixelSize",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": COLUMNS.installment_commission - 1,
                        "endIndex": LAST_COLUMN,
                    },
                    "properties": {"hiddenByUser": True},
                    "fields": "hiddenByUser",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": 0,
                        "endIndex": last_used_row,
                    },
                    "properties": {"pixelSize": 28},
                    "fields": "pixelSize",
                }
            },
        ]

        widths = {
            COLUMNS.source: 55,
            COLUMNS.tracking_number: 105,
            COLUMNS.shipment_status: 100,
            COLUMNS.order_date: 78,
            COLUMNS.order_number: 80,
            COLUMNS.customer: 135,
            COLUMNS.phone: 95,
            COLUMNS.product: 180,
            COLUMNS.product_code: 70,
            COLUMNS.sender: 85,
            COLUMNS.quantity: 60,
            COLUMNS.unit_price: 80,
            COLUMNS.line_total: 85,
            COLUMNS.order_total: 90,
            COLUMNS.payment_method: 90,
            COLUMNS.prepayment: 75,
            COLUMNS.cost: 85,
            COLUMNS.markup: 75,
            COLUMNS.advertising: 80,
            COLUMNS.net_profit: 85,
            COLUMNS.manager_note: 110,
        }
        for column, width in widths.items():
            requests.append(
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": column - 1,
                            "endIndex": column,
                        },
                        "properties": {"pixelSize": width},
                        "fields": "pixelSize",
                    }
                }
            )

        style_by_type = {
            ROW_MONTH: ("#17365D", "#FFFFFF", 11, 32),
            ROW_DAY: ("#D9EAF7", "#17365D", 9, 30),
            ROW_HEADER: ("#2F75B5", "#FFFFFF", 8, 64),
            ROW_REPORT_DAY: ("#E2F0D9", "#375623", 8, 34),
            ROW_REPORT_MTD: ("#DDEBF7", "#1F4E78", 8, 34),
            ROW_REPORT_FORECAST: ("#FCE4D6", "#9C5700", 8, 34),
        }
        for row_type, (fill_hex, font_hex, font_size, height) in style_by_type.items():
            for row_number in typed_rows.get(row_type, []):
                requests.extend(
                    [
                        {
                            "repeatCell": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "startRowIndex": row_number - 1,
                                    "endRowIndex": row_number,
                                    "startColumnIndex": 0,
                                    "endColumnIndex": LAST_COLUMN,
                                },
                                "cell": {
                                    "userEnteredFormat": {
                                        "backgroundColorStyle": {"rgbColor": self._hex_color(fill_hex)},
                                        "textFormat": {
                                            "bold": True,
                                            "foregroundColorStyle": {"rgbColor": self._hex_color(font_hex)},
                                            "fontSize": font_size,
                                        },
                                        "horizontalAlignment": "CENTER",
                                        "verticalAlignment": "MIDDLE",
                                        "wrapStrategy": "WRAP",
                                    }
                                },
                                "fields": "userEnteredFormat(backgroundColorStyle,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy)",
                            }
                        },
                        {
                            "updateDimensionProperties": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "dimension": "ROWS",
                                    "startIndex": row_number - 1,
                                    "endIndex": row_number,
                                },
                                "properties": {"pixelSize": height},
                                "fields": "pixelSize",
                            }
                        },
                    ]
                )
        for order_index, row_number in enumerate(typed_rows.get(ROW_ORDER, [])):
            requests.extend(
                [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_number - 1,
                            "endRowIndex": row_number,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(BUSINESS_HEADERS),
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColorStyle": {
                                    "rgbColor": self._hex_color("#F3F8FC" if order_index % 2 else "#FFFFFF")
                                },
                                "textFormat": {"fontFamily": "Arial", "fontSize": 8},
                                "borders": {
                                    "bottom": {
                                        "style": "SOLID",
                                        "colorStyle": {"rgbColor": self._hex_color("#D9E0E7")},
                                    }
                                },
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColorStyle,textFormat,borders.bottom)",
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": row_number - 1,
                            "endIndex": row_number,
                        },
                        "properties": {"pixelSize": 44},
                        "fields": "pixelSize",
                    }
                },
                ]
            )

        for group_rows in order_groups.values():
            if len(group_rows) < 2:
                continue
            requests.append(
                {
                    "updateBorders": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": min(group_rows) - 1,
                            "endRowIndex": max(group_rows),
                            "startColumnIndex": 0,
                            "endColumnIndex": len(BUSINESS_HEADERS),
                        },
                        "top": {
                            "style": "SOLID_MEDIUM",
                            "colorStyle": {"rgbColor": self._hex_color("#000000")},
                        },
                        "bottom": {
                            "style": "SOLID_MEDIUM",
                            "colorStyle": {"rgbColor": self._hex_color("#000000")},
                        },
                        "left": {
                            "style": "SOLID_MEDIUM",
                            "colorStyle": {"rgbColor": self._hex_color("#000000")},
                        },
                        "right": {
                            "style": "SOLID_MEDIUM",
                            "colorStyle": {"rgbColor": self._hex_color("#000000")},
                        },
                    }
                }
            )

        number_formats = {
            COLUMNS.tracking_number: ("TEXT", "@"),
            COLUMNS.order_date: ("DATE", "dd.mm.yyyy"),
            COLUMNS.order_number: ("TEXT", "@"),
            COLUMNS.phone: ("TEXT", "@"),
            COLUMNS.product_code: ("TEXT", "@"),
            COLUMNS.quantity: ("NUMBER", "0"),
            COLUMNS.unit_price: ("NUMBER", "#,##0.00"),
            COLUMNS.line_total: ("NUMBER", "#,##0.00"),
            COLUMNS.order_total: ("NUMBER", "#,##0.00"),
            COLUMNS.prepayment: ("NUMBER", "#,##0.00"),
            COLUMNS.cost: ("NUMBER", "#,##0.00"),
            COLUMNS.markup: ("NUMBER", "#,##0.00"),
            COLUMNS.advertising: ("NUMBER", "#,##0.00"),
            COLUMNS.net_profit: ("NUMBER", "#,##0.00"),
            COLUMNS.supplier_cost_original: ("NUMBER", "#,##0.00"),
            COLUMNS.operational_date: ("DATE", "dd.mm.yyyy"),
            COLUMNS.first_seen_completed: ("DATE", "dd.mm.yyyy"),
        }
        for column, (format_type, pattern) in number_formats.items():
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": row_count,
                            "startColumnIndex": column - 1,
                            "endColumnIndex": column,
                        },
                        "cell": {"userEnteredFormat": {"numberFormat": {"type": format_type, "pattern": pattern}}},
                        "fields": "userEnteredFormat.numberFormat",
                    }
                }
            )

        for row_number in typed_rows.get(ROW_DAY, []):
            requests.extend(
                [
                    {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": row_number - 1,
                            "endRowIndex": row_number,
                            "startColumnIndex": 1,
                            "endColumnIndex": 2,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {"type": "DATE", "pattern": "dd.mm.yyyy"}
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                    },
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": row_number - 1,
                                "endRowIndex": row_number,
                                "startColumnIndex": USD_RATE_LABEL_COLUMN - 1,
                                "endColumnIndex": USD_RATE_VALUE_COLUMN,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColorStyle": {
                                        "rgbColor": self._hex_color("#FFF2CC")
                                    },
                                    "textFormat": {"bold": True, "fontSize": 9},
                                }
                            },
                            "fields": "userEnteredFormat(backgroundColorStyle,textFormat)",
                        }
                    },
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": row_number - 1,
                                "endRowIndex": row_number,
                                "startColumnIndex": USD_RATE_VALUE_COLUMN - 1,
                                "endColumnIndex": USD_RATE_VALUE_COLUMN,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "numberFormat": {"type": "NUMBER", "pattern": "0.00"}
                                }
                            },
                            "fields": "userEnteredFormat.numberFormat",
                        }
                    },
                    {
                        "setDataValidation": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": row_number - 1,
                                "endRowIndex": row_number,
                                "startColumnIndex": USD_RATE_VALUE_COLUMN - 1,
                                "endColumnIndex": USD_RATE_VALUE_COLUMN,
                            },
                            "rule": {
                                "condition": {
                                    "type": "NUMBER_BETWEEN",
                                    "values": [
                                        {"userEnteredValue": str(_MIN_USD_RATE)},
                                        {"userEnteredValue": str(_MAX_USD_RATE)},
                                    ],
                                },
                                "strict": True,
                                "showCustomUi": True,
                            },
                        }
                    },
                ]
            )

        for row_type in (ROW_MONTH, ROW_DAY):
            for row_number in typed_rows.get(row_type, []):
                requests.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": row_number - 1,
                                "endRowIndex": row_number,
                                "startColumnIndex": 2,
                                "endColumnIndex": 3,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "backgroundColorStyle": {"rgbColor": self._hex_color("#2F75B5")},
                                    "textFormat": {
                                        "bold": True,
                                        "fontSize": 8,
                                        "foregroundColorStyle": {"rgbColor": self._hex_color("#FFFFFF")},
                                    },
                                }
                            },
                            "fields": "userEnteredFormat(backgroundColorStyle,textFormat)",
                        }
                    }
                )

        for row_type in (ROW_REPORT_DAY, ROW_REPORT_MTD, ROW_REPORT_FORECAST):
            for row_number in typed_rows.get(row_type, []):
                for column in (4, 6, 8, 10, 12, 14, 16, 18, 20):
                    pattern = "0" if column == 4 else "#,##0.00"
                    requests.append(
                        {
                            "repeatCell": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "startRowIndex": row_number - 1,
                                    "endRowIndex": row_number,
                                    "startColumnIndex": column - 1,
                                    "endColumnIndex": column,
                                },
                                "cell": {
                                    "userEnteredFormat": {
                                        "numberFormat": {"type": "NUMBER", "pattern": pattern}
                                    }
                                },
                                "fields": "userEnteredFormat.numberFormat",
                            }
                        }
                    )

        metadata = self.spreadsheet.fetch_sheet_metadata(
            {"fields": "sheets(properties(sheetId),conditionalFormats)"}
        )
        conditional_count = next(
            (
                len(sheet.get("conditionalFormats", []))
                for sheet in metadata.get("sheets", [])
                if sheet.get("properties", {}).get("sheetId") == sheet_id
            ),
            0,
        )
        requests.extend(
            {"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": index}}
            for index in reversed(range(conditional_count))
        )
        requests.extend(self._conditional_format_requests(row_count))
        # The month-row style is applied after the base font and would otherwise
        # enlarge A1:D1 back to 11 pt. Keep the compact navigation row last so it
        # always wins regardless of the month formatting above.
        requests.extend(
            [
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": 4,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "textFormat": {"fontSize": 8},
                                "wrapStrategy": "WRAP",
                            }
                        },
                        "fields": (
                            "userEnteredFormat.textFormat.fontSize,"
                            "userEnteredFormat.wrapStrategy"
                        ),
                    }
                },
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": 0,
                            "endIndex": 1,
                        },
                        "properties": {"pixelSize": 24},
                        "fields": "pixelSize",
                    }
                },
            ]
        )
        self.spreadsheet.batch_update({"requests": requests})

    def _conditional_format_requests(self, row_count: int) -> list[dict[str, Any]]:
        sheet_id = self.worksheet.id
        data_range = {
            "sheetId": sheet_id,
            "startRowIndex": self.header_row,
            "endRowIndex": row_count,
            "startColumnIndex": 0,
            "endColumnIndex": len(BUSINESS_HEADERS),
        }
        status_range = dict(data_range, startColumnIndex=COLUMNS.shipment_status - 1, endColumnIndex=COLUMNS.shipment_status)

        def rule(text: str, color: str) -> dict[str, Any]:
            return {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [status_range],
                        "booleanRule": {
                            "condition": {"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": text}]},
                            "format": {"backgroundColorStyle": {"rgbColor": self._hex_color(color)}},
                        },
                    },
                    "index": 0,
                }
            }

        reporting_column = "".join(
            character
            for character in rowcol_to_a1(1, COLUMNS.reporting_state)
            if character.isalpha()
        )
        refusal_block_rule = {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [data_range],
                    "booleanRule": {
                        "condition": {
                            "type": "CUSTOM_FORMULA",
                            "values": [
                                {
                                    "userEnteredValue": (
                                        f'=${reporting_column}{self.header_row + 1}='
                                        f'"{REPORTING_EXCLUDED_REFUSAL}"'
                                    )
                                }
                            ],
                        },
                        "format": {
                            "backgroundColorStyle": {
                                "rgbColor": self._hex_color("#F4CCCC")
                            }
                        },
                    },
                },
                "index": 0,
            }
        }
        net_profit_range = dict(
            data_range,
            startColumnIndex=COLUMNS.net_profit - 1,
            endColumnIndex=COLUMNS.net_profit,
        )
        negative_net_profit_rule = {
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [net_profit_range],
                    "booleanRule": {
                        "condition": {
                            "type": "NUMBER_LESS",
                            "values": [{"userEnteredValue": "0"}],
                        },
                        "format": {
                            "backgroundColorStyle": {
                                "rgbColor": self._hex_color("#F4CCCC")
                            },
                            "textFormat": {
                                "bold": True,
                                "foregroundColorStyle": {
                                    "rgbColor": self._hex_color("#9C0006")
                                },
                            },
                        },
                    },
                },
                "index": 0,
            }
        }
        return [
            rule("Відмова", "#F4CCCC"),
            rule("Повернуто", "#F4CCCC"),
            rule("Відправлення отримано", "#D9EAD3"),
            rule("Отримано", "#D9EAD3"),
            rule("дорозі", "#FFF2CC"),
            rule("Прибуло", "#FFF2CC"),
            refusal_block_rule,
            negative_net_profit_rule,
        ]

    @staticmethod
    def _hex_color(value: str) -> dict[str, float]:
        value = value.lstrip("#")
        return {
            "red": int(value[0:2], 16) / 255,
            "green": int(value[2:4], 16) / 255,
            "blue": int(value[4:6], 16) / 255,
        }

    def read_existing_sync_keys(self) -> set[str]:
        values = self.worksheet.get_all_values()
        keys: set[str] = set()
        for row in values:
            row_type = str(row[COLUMNS.row_type - 1]).strip() if len(row) >= COLUMNS.row_type else ""
            if row_type and row_type != ROW_ORDER:
                continue
            if len(row) >= COLUMNS.sync_key and str(row[COLUMNS.sync_key - 1]).strip():
                key = str(row[COLUMNS.sync_key - 1]).strip().casefold()
                if key != "sync key":
                    keys.add(key)
                continue
            source = source_key(row[COLUMNS.source - 1]) if len(row) >= COLUMNS.source else ""
            order_id = str(row[COLUMNS.order_number - 1]).strip() if len(row) >= COLUMNS.order_number else ""
            if source and order_id:
                keys.add(f"{source.casefold()}:{order_id}")
        return keys

    def latest_layout_day(self) -> date | None:
        """Return the newest explicit day section without reading business columns."""
        row_type_column = rowcol_to_a1(1, COLUMNS.row_type).rstrip("1")
        operational_date_column = rowcol_to_a1(
            1, COLUMNS.operational_date
        ).rstrip("1")
        values = self.worksheet.get(
            f"{row_type_column}1:{operational_date_column}{self.worksheet.row_count}",
            value_render_option="UNFORMATTED_VALUE",
        )
        days = [
            parsed
            for row in values
            if row and str(row[0]).strip() == ROW_DAY
            if (parsed := parse_sheet_date(row[1] if len(row) > 1 else "")) is not None
        ]
        return max(days, default=None)

    def needs_refusal_reconciliation(
        self,
        *,
        supplier_prepayment_tracking_keys: set[str] | None = None,
        quarantine_unverified_refusals: bool = False,
    ) -> bool:
        """Return whether a refused block still needs removal or exclusion marking."""
        groups: dict[str, list[list[Any]]] = {}
        for row in self.worksheet.get_all_values(value_render_option="FORMULA"):
            if len(row) < COLUMNS.sync_key:
                continue
            if str(row[COLUMNS.row_type - 1]).strip() != ROW_ORDER:
                continue
            key = str(row[COLUMNS.sync_key - 1]).strip().casefold()
            if key:
                groups.setdefault(key, []).append(row)
        supplier_prepayments = supplier_prepayment_tracking_keys or set()
        for rows in groups.values():
            if not any(row_is_refused(row) for row in rows):
                continue
            has_supplier_prepayment = any(
                tracking_match_key(
                    row[COLUMNS.tracking_number - 1]
                    if len(row) >= COLUMNS.tracking_number
                    else ""
                )
                in supplier_prepayments
                for row in rows
            )
            has_prepayment = has_supplier_prepayment or any(
                row_has_prepayment(row) for row in rows
            )
            if not has_prepayment and not quarantine_unverified_refusals:
                return True
            if not has_prepayment and all(
                len(row) >= COLUMNS.reporting_state
                and str(row[COLUMNS.reporting_state - 1]).strip()
                == REPORTING_EXCLUDED_REFUSAL
                for row in rows
            ):
                continue
            if any(
                len(row) < COLUMNS.reporting_state
                or str(row[COLUMNS.reporting_state - 1]).strip()
                != REPORTING_EXCLUDED_REFUSAL
                for row in rows
            ):
                return True
        return False

    def pending_tracking_numbers(self) -> list[str]:
        values = self.worksheet.get_all_values()
        numbers: list[str] = []
        for row in values:
            row_type = str(row[COLUMNS.row_type - 1]).strip() if len(row) >= COLUMNS.row_type else ""
            if row_type and row_type != ROW_ORDER:
                continue
            raw_tracking = (
                str(row[COLUMNS.tracking_number - 1]).strip()
                if len(row) >= COLUMNS.tracking_number
                else ""
            )
            tracking = extract_ttn(raw_tracking)
            status = (
                str(row[COLUMNS.shipment_status - 1]).strip().casefold()
                if len(row) >= COLUMNS.shipment_status
                else ""
            )
            if tracking and not self._is_final_status(status):
                numbers.append(tracking)
        return list(dict.fromkeys(numbers))

    @staticmethod
    def _is_final_status(status: str) -> bool:
        return "отриман" in status or "відмова від отримання" in status

    def update_shipment_statuses(self, statuses: dict[str, ShipmentStatus]) -> ShipmentUpdateResult:
        if not statuses:
            return ShipmentUpdateResult()
        values = self.worksheet.get_all_values()
        updates: list[dict[str, Any]] = []
        changes: dict[str, ShipmentStatusChange] = {}
        for row_number, row in enumerate(values, start=1):
            row_type = str(row[COLUMNS.row_type - 1]).strip() if len(row) >= COLUMNS.row_type else ""
            if row_type and row_type != ROW_ORDER:
                continue
            raw_tracking = (
                str(row[COLUMNS.tracking_number - 1]).strip()
                if len(row) >= COLUMNS.tracking_number
                else ""
            )
            tracking = extract_ttn(raw_tracking)
            status = statuses.get(tracking)
            if not status:
                continue
            current = (
                str(row[COLUMNS.shipment_status - 1]).strip()
                if len(row) >= COLUMNS.shipment_status
                else ""
            )
            if current != status.status:
                updates.append(
                    {
                        "range": rowcol_to_a1(row_number, COLUMNS.shipment_status),
                        "values": [[status.status]],
                    }
                )
                sync_key = (
                    str(row[COLUMNS.sync_key - 1]).strip()
                    if len(row) >= COLUMNS.sync_key
                    else ""
                )
                source = source_key(row[COLUMNS.source - 1] if len(row) >= COLUMNS.source else "")
                order_id = (
                    str(row[COLUMNS.order_number - 1]).strip()
                    if len(row) >= COLUMNS.order_number
                    else ""
                )
                if not order_id and ":" in sync_key:
                    order_id = sync_key.split(":", 1)[1]
                change_key = sync_key.casefold() or f"{source}:{order_id}:{tracking}"
                changes.setdefault(
                    change_key,
                    ShipmentStatusChange(
                        source=source,
                        order_id=order_id,
                        sync_key=sync_key,
                        tracking_number=raw_tracking,
                        old_status=current,
                        new_status=status.status,
                    ),
                )
        if updates:
            self.worksheet.batch_update(updates, raw=True)
        return ShipmentUpdateResult(cell_updates=len(updates), changes=tuple(changes.values()))

    def refresh_order_details(self, orders: list[Order]) -> int:
        order_by_key = {order.sync_key.casefold(): order for order in orders}
        values = self.worksheet.get_all_values(value_render_option="FORMULA")
        rows_by_key: dict[str, list[tuple[int, list[str]]]] = {}
        for row_number, row in enumerate(values, start=1):
            row_type = str(row[COLUMNS.row_type - 1]).strip() if len(row) >= COLUMNS.row_type else ""
            if row_type != ROW_ORDER:
                continue
            key = (
                str(row[COLUMNS.sync_key - 1]).strip().casefold()
                if len(row) >= COLUMNS.sync_key
                else ""
            )
            if key:
                rows_by_key.setdefault(key, []).append((row_number, row))

        updates: list[dict[str, Any]] = []
        for key, sheet_rows in rows_by_key.items():
            order = order_by_key.get(key)
            customer = ""
            if order:
                customer = customer_display(order.city, order.customer_name)
            for item_index, (row_number, row) in enumerate(sheet_rows):
                if order and order.channel:
                    desired_source = source_display(order.channel or order.source)
                    current_source = (
                        str(row[COLUMNS.source - 1]).strip()
                        if len(row) >= COLUMNS.source
                        else ""
                    )
                    if current_source != desired_source:
                        updates.append(
                            {
                                "range": rowcol_to_a1(row_number, COLUMNS.source),
                                "values": [[desired_source]],
                            }
                        )
                if order and order.tracking_number:
                    current = (
                        str(row[COLUMNS.tracking_number - 1]).strip()
                        if len(row) >= COLUMNS.tracking_number
                        else ""
                    )
                    if current != order.tracking_number:
                        updates.append(
                            {
                                "range": rowcol_to_a1(row_number, COLUMNS.tracking_number),
                                "values": [[order.tracking_number]],
                            }
                        )
                # Existing Rozetka rows keep the date on which they first
                # entered the CRM. Shipment-status refreshes may update the
                # status/details, but must never move rows between day blocks.
                if (
                    order
                    and order.completion_is_exact
                    and order.source.casefold() != "rozetka"
                ):
                    completion_date = order.completed_at.date()
                    current_completion_date = parse_sheet_date(
                        row[COLUMNS.order_date - 1] if len(row) >= COLUMNS.order_date else ""
                    )
                    if current_completion_date != completion_date:
                        updates.append(
                            {
                                "range": rowcol_to_a1(row_number, COLUMNS.order_date),
                                "values": [[sheet_serial(completion_date)]],
                            }
                        )
                    current_day = parse_sheet_date(
                        row[COLUMNS.operational_date - 1]
                        if len(row) >= COLUMNS.operational_date
                        else ""
                    )
                    if current_day != completion_date:
                        updates.append(
                            {
                                "range": rowcol_to_a1(row_number, COLUMNS.operational_date),
                                "values": [[sheet_serial(completion_date)]],
                            }
                        )
                if customer:
                    current = str(row[COLUMNS.customer - 1]).strip() if len(row) >= COLUMNS.customer else ""
                    if current != customer:
                        updates.append(
                            {
                                "range": rowcol_to_a1(row_number, COLUMNS.customer),
                                "values": [[customer]],
                            }
                        )
                if order and item_index < len(order.items):
                    item = order.items[item_index]
                    if item.product_code:
                        product_code = item.product_code
                        current = (
                            str(row[COLUMNS.product_code - 1]).strip()
                            if len(row) >= COLUMNS.product_code
                            else ""
                        )
                        if current != product_code:
                            updates.append(
                                {
                                    "range": rowcol_to_a1(row_number, COLUMNS.product_code),
                                    "values": [[product_code]],
                                }
                            )
                    numeric_updates = {
                        COLUMNS.quantity: item.quantity,
                        COLUMNS.unit_price: item.unit_price,
                        COLUMNS.line_total: item.line_total,
                    }
                    existing_quantity = decimal_value(
                        row[COLUMNS.quantity - 1] if len(row) >= COLUMNS.quantity else ""
                    )
                    existing_line_total = decimal_value(
                        row[COLUMNS.line_total - 1] if len(row) >= COLUMNS.line_total else ""
                    )
                    # Some historical Prom payloads exposed price=1 while the
                    # line total was correct. Repair the source value from the
                    # already trusted line total until the next full rebuild.
                    if (
                        numeric_updates[COLUMNS.unit_price] <= 1
                        and existing_quantity > 0
                        and existing_line_total > 1
                    ):
                        numeric_updates[COLUMNS.unit_price] = existing_line_total / existing_quantity
                    for column, expected in numeric_updates.items():
                        current = row[column - 1] if len(row) >= column else ""
                        if decimal_value(current) != expected:
                            updates.append(
                                {
                                    "range": rowcol_to_a1(row_number, column),
                                    "values": [[decimal_for_sheet(expected)]],
                                }
                            )
                if order and item_index == 0:
                    current_total = row[COLUMNS.order_total - 1] if len(row) >= COLUMNS.order_total else ""
                    if decimal_value(current_total) != order.total:
                        updates.append(
                            {
                                "range": rowcol_to_a1(row_number, COLUMNS.order_total),
                                "values": [[decimal_for_sheet(order.total)]],
                            }
                        )
                    current_base = (
                        row[COLUMNS.advertising_base - 1]
                        if len(row) >= COLUMNS.advertising_base
                        else ""
                    )
                    current_installment = (
                        row[COLUMNS.installment_commission - 1]
                        if len(row) >= COLUMNS.installment_commission
                        else ""
                    )
                    current_installment_source = (
                        str(row[COLUMNS.installment_commission_source - 1]).strip()
                        if len(row) >= COLUMNS.installment_commission_source
                        else ""
                    )
                    existing_installment = decimal_value(current_installment)
                    effective_installment = order.installment_commission
                    if (
                        order.payment_method == "оплата частями"
                        and effective_installment == 0
                        and existing_installment > 0
                    ):
                        effective_installment = existing_installment
                    incoming_source = order.installment_commission_source or (
                        "reported" if effective_installment > 0 else ""
                    )
                    source_rank = {"fallback": 1, "tariff": 2, "reported": 3}
                    current_rank = source_rank.get(
                        current_installment_source,
                        3 if existing_installment > 0 else 0,
                    )
                    incoming_rank = source_rank.get(incoming_source, 0)
                    if existing_installment > 0 and incoming_rank < current_rank:
                        effective_installment = existing_installment
                    effective_source = current_installment_source
                    if effective_installment == order.installment_commission:
                        effective_source = incoming_source
                    if decimal_value(current_base) != order.advertising_cost:
                        updates.append(
                            {
                                "range": rowcol_to_a1(row_number, COLUMNS.advertising_base),
                                "values": [[decimal_for_sheet(order.advertising_cost)]],
                            }
                        )
                    if existing_installment != effective_installment:
                        updates.append(
                            {
                                "range": rowcol_to_a1(row_number, COLUMNS.installment_commission),
                                "values": [[decimal_for_sheet(effective_installment)]],
                            }
                        )
                    if current_installment_source != effective_source:
                        updates.append(
                            {
                                "range": rowcol_to_a1(
                                    row_number,
                                    COLUMNS.installment_commission_source,
                                ),
                                "values": [[effective_source]],
                            }
                        )
                    expected_advertising = advertising_display(
                        order.advertising_cost, effective_installment
                    )
                    current_advertising = (
                        row[COLUMNS.advertising - 1]
                        if len(row) >= COLUMNS.advertising
                        else ""
                    )
                    if str(current_advertising) != str(expected_advertising):
                        updates.append(
                            {
                                "range": rowcol_to_a1(row_number, COLUMNS.advertising),
                                "values": [[expected_advertising]],
                            }
                        )
                    prepayment = order.prepayment or parse_prepayment(order.note)
                    current_prepayment = (
                        row[COLUMNS.prepayment - 1]
                        if len(row) >= COLUMNS.prepayment
                        else ""
                    )
                    if prepayment > 0 and decimal_value(current_prepayment) == 0:
                        updates.append(
                            {
                                "range": rowcol_to_a1(row_number, COLUMNS.prepayment),
                                "values": [[decimal_for_sheet(prepayment)]],
                            }
                        )
                if order and order.payment_method:
                    current = (
                        str(row[COLUMNS.payment_method - 1]).strip()
                        if len(row) >= COLUMNS.payment_method
                        else ""
                    )
                    if current != order.payment_method:
                        updates.append(
                            {
                                "range": rowcol_to_a1(row_number, COLUMNS.payment_method),
                                "values": [[order.payment_method]],
                            }
                        )
                formula = markup_formula(row_number)
                current_formula = (
                    str(row[COLUMNS.markup - 1]).strip() if len(row) >= COLUMNS.markup else ""
                )
                # A structural rebuild repairs every historical formula. Limit
                # pre-rebuild cell updates to orders present in this API batch
                # so a formula syntax migration does not create thousands of
                # redundant ranges before rewriting the sheet once.
                if order and current_formula != formula:
                    updates.append(
                        {
                            "range": rowcol_to_a1(row_number, COLUMNS.markup),
                            "values": [[formula]],
                        }
                    )
                net_formula = net_profit_formula(row_number)
                current_net_formula = (
                    str(row[COLUMNS.net_profit - 1]).strip()
                    if len(row) >= COLUMNS.net_profit
                    else ""
                )
                if order and current_net_formula != net_formula:
                    updates.append(
                        {
                            "range": rowcol_to_a1(row_number, COLUMNS.net_profit),
                            "values": [[net_formula]],
                        }
                    )

        if updates:
            self.worksheet.batch_update(updates, raw=False)
        return len(updates)

    def record_completion_observations(
        self,
        orders: list[Order],
        *,
        observed_at: datetime,
    ) -> tuple[OrderAuditEvent, ...]:
        if not orders:
            return ()
        order_by_key = {order.sync_key.casefold(): order for order in orders}
        values = self.worksheet.get_all_values(value_render_option="FORMULA")
        updates: list[dict[str, Any]] = []
        events: list[OrderAuditEvent] = []
        event_keys: set[str] = set()

        for row_number, row in enumerate(values, start=1):
            row_type = str(row[COLUMNS.row_type - 1]).strip() if len(row) >= COLUMNS.row_type else ""
            if row_type != ROW_ORDER:
                continue
            key = (
                str(row[COLUMNS.sync_key - 1]).strip().casefold()
                if len(row) >= COLUMNS.sync_key
                else ""
            )
            order = order_by_key.get(key)
            if not order:
                continue

            stored_day = parse_sheet_date(
                row[COLUMNS.operational_date - 1] if len(row) >= COLUMNS.operational_date else ""
            )
            first_seen = parse_sheet_date(
                row[COLUMNS.first_seen_completed - 1]
                if len(row) >= COLUMNS.first_seen_completed
                else ""
            )
            if order.is_completed and first_seen is None:
                first_seen = (
                    order.completed_at.date()
                    if order.completion_is_exact
                    else stored_day or observed_at.date()
                )
                updates.append(
                    {
                        "range": rowcol_to_a1(row_number, COLUMNS.first_seen_completed),
                        "values": [[sheet_serial(first_seen)]],
                    }
                )

            current_completion = parse_sheet_date(
                row[COLUMNS.order_date - 1] if len(row) >= COLUMNS.order_date else ""
            )
            desired_status_day = (
                order.completed_at.date()
                if order.completion_is_exact
                else stored_day or observed_at.date()
            )
            if (
                order.source.casefold() != "rozetka"
                and current_completion != desired_status_day
            ):
                updates.append(
                    {
                        "range": rowcol_to_a1(row_number, COLUMNS.order_date),
                        "values": [[sheet_serial(desired_status_day)]],
                    }
                )

            old_status = (
                str(row[COLUMNS.order_status - 1]).strip()
                if len(row) >= COLUMNS.order_status
                else ""
            )
            desired_status = order.source_status
            if old_status != desired_status:
                updates.append(
                    {
                        "range": rowcol_to_a1(row_number, COLUMNS.order_status),
                        "values": [[desired_status]],
                    }
                )
                if old_status and key not in event_keys:
                    events.append(
                        OrderAuditEvent(
                            occurred_at=observed_at,
                            event_type="Змінено статус замовлення",
                            source=order.source,
                            order_id=order.external_id,
                            sync_key=order.sync_key,
                            tracking_number=order.tracking_number,
                            field="Статус замовлення",
                            old_value=old_status,
                            new_value=desired_status,
                            details="Зафіксовано під час синхронізації джерела",
                        )
                    )
                    event_keys.add(key)

        if updates:
            self.worksheet.batch_update(updates, raw=False)
        return tuple(events)

    def backfill_completion_state(self, *, observed_at: datetime) -> int:
        values = self.worksheet.get_all_values(value_render_option="FORMULA")
        updates: list[dict[str, Any]] = []
        for row_number, row in enumerate(values, start=1):
            row_type = str(row[COLUMNS.row_type - 1]).strip() if len(row) >= COLUMNS.row_type else ""
            marker_source = str(row[0]).strip().casefold() if row else ""
            marker_ttn = str(row[1]).strip().casefold() if len(row) > 1 else ""
            if row_type == ROW_HEADER or (
                marker_source == BUSINESS_HEADERS[0].casefold()
                and marker_ttn == BUSINESS_HEADERS[1].casefold()
            ):
                current_headers = list(row[COLUMNS.sync_key - 1 : LAST_COLUMN])
                expected_headers = list(ALL_HEADERS[COLUMNS.sync_key - 1 :])
                if current_headers != expected_headers:
                    updates.append(
                        {
                            "range": (
                                f"{rowcol_to_a1(row_number, COLUMNS.sync_key)}:"
                                f"{rowcol_to_a1(row_number, LAST_COLUMN)}"
                            ),
                            "values": [expected_headers],
                        }
                    )
                continue
            if row_type != ROW_ORDER:
                continue

            first_seen = parse_sheet_date(
                row[COLUMNS.first_seen_completed - 1]
                if len(row) >= COLUMNS.first_seen_completed
                else ""
            )
            completion_day = parse_sheet_date(
                row[COLUMNS.order_date - 1] if len(row) >= COLUMNS.order_date else ""
            )
            operational_day = parse_sheet_date(
                row[COLUMNS.operational_date - 1] if len(row) >= COLUMNS.operational_date else ""
            )
            if first_seen is None:
                first_seen = completion_day or operational_day or observed_at.date()
                updates.append(
                    {
                        "range": rowcol_to_a1(row_number, COLUMNS.first_seen_completed),
                        "values": [[sheet_serial(first_seen)]],
                    }
                )
            if completion_day is None:
                updates.append(
                    {
                        "range": rowcol_to_a1(row_number, COLUMNS.order_date),
                        "values": [[sheet_serial(first_seen)]],
                    }
                )
            current_status = (
                str(row[COLUMNS.order_status - 1]).strip()
                if len(row) >= COLUMNS.order_status
                else ""
            )
            if not current_status:
                updates.append(
                    {
                        "range": rowcol_to_a1(row_number, COLUMNS.order_status),
                        "values": [["Виконано"]],
                    }
                )

        if updates:
            self.worksheet.batch_update(updates, raw=False)
        return len(updates)

    def update_order_expenses(self, expenses: dict[str, Decimal], *, source: str) -> int:
        if not expenses:
            return 0
        values = self.worksheet.get_all_values(value_render_option="FORMULA")
        rows_by_order: dict[str, list[tuple[int, list[Any]]]] = {}
        wanted_source = source_key(source)
        for row_number, row in enumerate(values, start=1):
            row_type = str(row[COLUMNS.row_type - 1]).strip() if len(row) >= COLUMNS.row_type else ""
            if row_type != ROW_ORDER:
                continue
            row_source = source_key(row[COLUMNS.source - 1]) if len(row) >= COLUMNS.source else ""
            if row_source != wanted_source:
                continue
            sync_key = str(row[COLUMNS.sync_key - 1]).strip() if len(row) >= COLUMNS.sync_key else ""
            order_id = sync_key.split(":", 1)[1].strip() if ":" in sync_key else ""
            if not order_id and len(row) >= COLUMNS.order_number:
                order_id = str(row[COLUMNS.order_number - 1]).strip()
            if order_id in expenses:
                rows_by_order.setdefault(order_id, []).append((row_number, row))

        updates: list[dict[str, Any]] = []
        for order_id, order_rows in rows_by_order.items():
            expected = max(Decimal(0), expenses[order_id])
            for index, (row_number, row) in enumerate(order_rows):
                current = (
                    row[COLUMNS.advertising_base - 1]
                    if len(row) >= COLUMNS.advertising_base
                    else ""
                )
                if index == 0:
                    if decimal_value(current) != expected:
                        updates.append(
                            {
                                "range": rowcol_to_a1(row_number, COLUMNS.advertising_base),
                                "values": [[decimal_for_sheet(expected)]],
                            }
                        )
                    display = advertising_display(expected, Decimal(0))
                    shown = row[COLUMNS.advertising - 1] if len(row) >= COLUMNS.advertising else ""
                    if str(shown) != str(display):
                        updates.append(
                            {
                                "range": rowcol_to_a1(row_number, COLUMNS.advertising),
                                "values": [[display]],
                            }
                        )
                else:
                    if str(current).strip():
                        updates.append(
                            {
                                "range": rowcol_to_a1(row_number, COLUMNS.advertising_base),
                                "values": [[""]],
                            }
                        )
                    if len(row) >= COLUMNS.advertising and str(row[COLUMNS.advertising - 1]).strip():
                        updates.append(
                            {
                                "range": rowcol_to_a1(row_number, COLUMNS.advertising),
                                "values": [[""]],
                            }
                        )
        if updates:
            self.worksheet.batch_update(updates, raw=False)
        return len(updates)

    def update_supplier_costs(
        self,
        costs: Mapping[SupplierCostKey, ResolvedSupplierCost],
        *,
        observed_at: datetime,
    ) -> SupplierCostUpdateResult:
        """Fill supplier costs, converting Melad USD values at the CRM day rate."""
        values = self.worksheet.get_all_values(value_render_option="FORMULA")
        normalized_costs = _normalize_supplier_costs(costs)
        normalized_costs = _with_melad_provenance_fallbacks(values, normalized_costs)
        if not normalized_costs:
            return SupplierCostUpdateResult()
        candidates, warnings = _discover_supplier_cost_candidates(
            values, normalized_costs
        )
        if not candidates:
            return SupplierCostUpdateResult(warnings=tuple(warnings))

        latest_values = self.worksheet.get_all_values(value_render_option="FORMULA")
        _, latest_rates, _ = _daily_usd_rates(latest_values)
        primary_update_bundles: list[list[dict[str, Any]]] = []
        formula_updates: list[dict[str, Any]] = []
        events: list[OrderAuditEvent] = []
        changed_costs = 0
        for candidate in candidates:
            if candidate.row_number > len(latest_values):
                continue
            row = candidate.original_row
            latest_row = latest_values[candidate.row_number - 1]
            key = candidate.key
            expected = candidate.expected
            latest_tracking = _cell_value(latest_row, COLUMNS.tracking_number)
            latest_product_code = _cell_value(latest_row, COLUMNS.product_code)
            latest_sender = _cell_value(latest_row, COLUMNS.sender)
            latest_cost = _cell_value(latest_row, COLUMNS.cost)
            latest_markup = _cell_value(latest_row, COLUMNS.markup)
            latest_net_profit = _cell_value(latest_row, COLUMNS.net_profit)
            latest_source = str(
                _cell_value(latest_row, COLUMNS.supplier_cost_source)
            ).strip()
            if (
                tracking_match_key(latest_tracking) != key.tracking_number
                or product_code_match_key(latest_product_code) != key.product_code
            ):
                continue
            if str(latest_cost).strip() and latest_source != expected.source:
                continue
            if expected.record.currency == "USD":
                concurrent_rate = latest_rates.get(candidate.operational_day)
                if concurrent_rate is None or concurrent_rate != candidate.rate:
                    warnings.append(
                        f"Melad cost for TTN {key.tracking_number} was skipped because the daily USD rate changed concurrently"
                    )
                    continue
            sheet_value = _supplier_sheet_value(expected, candidate.rate)
            cost_changed = not _supplier_cost_values_equal(
                latest_cost, sheet_value, expected
            )
            row_updates: list[dict[str, Any]] = []
            if cost_changed:
                changed_costs += 1
                _append_supplier_cost_cell_updates(
                    row_updates,
                    formula_updates,
                    row_number=candidate.row_number,
                    expected=expected,
                    sheet_value=sheet_value,
                )
            expected_formulas = (
                (COLUMNS.markup, latest_markup, markup_formula(candidate.row_number)),
                (
                    COLUMNS.net_profit,
                    latest_net_profit,
                    net_profit_formula(candidate.row_number),
                ),
            )
            formula_needs_repair = False
            if not cost_changed:
                for column, current_formula, expected_formula in expected_formulas:
                    if str(current_formula).strip() == expected_formula:
                        continue
                    formula_needs_repair = True
                    formula_updates.append(
                        {
                            "range": rowcol_to_a1(candidate.row_number, column),
                            "values": [[expected_formula]],
                        }
                    )
            supplier_sender = SUPPLIER_SENDER_DEFAULTS.get(
                expected.source, expected.sender
            )
            assign_supplier_sender = (
                bool(supplier_sender) and str(latest_sender).strip() != supplier_sender
            )
            if assign_supplier_sender:
                row_updates.append(
                    {
                        "range": rowcol_to_a1(candidate.row_number, COLUMNS.sender),
                        "values": [[supplier_sender]],
                    }
                )
            if not cost_changed and not assign_supplier_sender and not formula_needs_repair:
                continue
            sync_key = str(row[COLUMNS.sync_key - 1]).strip() if len(row) >= COLUMNS.sync_key else ""
            source = source_key(row[COLUMNS.source - 1]) if len(row) >= COLUMNS.source else ""
            order_id = (
                str(row[COLUMNS.order_number - 1]).strip()
                if len(row) >= COLUMNS.order_number
                else ""
            )
            is_text_marker = expected.record.kind == "text"
            if cost_changed:
                events.append(
                    OrderAuditEvent(
                        occurred_at=observed_at,
                        event_type=(
                            "supplier_cost_marker_filled"
                            if is_text_marker
                            else "supplier_cost_filled"
                        ),
                        source=source,
                        order_id=order_id,
                        sync_key=sync_key,
                        tracking_number=normalize_tracking_number(latest_tracking),
                        field="supplier_cost_marker" if is_text_marker else "unit_cost",
                        old_value=str(latest_cost),
                        new_value=str(sheet_value),
                        details=(
                            f"{expected.source}; USD {expected.record.unit_cost} × rate {candidate.rate}"
                            if expected.record.currency == "USD"
                            else expected.source
                        ),
                    )
                )
            if assign_supplier_sender:
                events.append(
                    OrderAuditEvent(
                        occurred_at=observed_at,
                        event_type="supplier_sender_assigned",
                        source=source,
                        order_id=order_id,
                        sync_key=sync_key,
                        tracking_number=normalize_tracking_number(latest_tracking),
                        field="sender",
                        old_value=str(latest_sender),
                        new_value=supplier_sender,
                        details=expected.source,
                    )
                )
            if row_updates:
                primary_update_bundles.append(row_updates)

        bundles_per_request = 100
        for offset in range(0, len(primary_update_bundles), bundles_per_request):
            bundled_updates = [
                update
                for bundle in primary_update_bundles[
                    offset : offset + bundles_per_request
                ]
                for update in bundle
            ]
            self.worksheet.batch_update(
                bundled_updates,
                raw=True,
            )
        updates_per_request = 400
        for offset in range(0, len(formula_updates), updates_per_request):
            self.worksheet.batch_update(
                formula_updates[offset : offset + updates_per_request], raw=False
            )
        LOGGER.info("Updated %s supplier cost cell(s)", changed_costs)
        return SupplierCostUpdateResult(
            cell_updates=changed_costs,
            audit_events=tuple(events),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def append_orders(
        self,
        orders: list[Order],
        shipment_statuses: dict[str, ShipmentStatus],
        *,
        sender_default: str,
        operational_day: date,
        observed_at: datetime | None = None,
        force_rebuild: bool = False,
        excluded_sync_keys: set[str] | None = None,
        supplier_prepayment_tracking_keys: set[str] | None = None,
        quarantine_unverified_refusals: bool = False,
    ) -> int:
        if not orders and not force_rebuild:
            return 0
        observation_day = (observed_at.date() if observed_at else operational_day)
        existing_values = self.worksheet.get_all_values(value_render_option="FORMULA")
        daily_usd_rates = _extract_daily_usd_rate_values(existing_values)
        order_groups = collect_order_groups(
            existing_values,
            orders,
            shipment_statuses,
            sender_default=sender_default,
            observation_day=observation_day,
            excluded_sync_keys=excluded_sync_keys,
            supplier_prepayment_tracking_keys=supplier_prepayment_tracking_keys,
            quarantine_unverified_refusals=quarantine_unverified_refusals,
        )
        snapshot = build_sheet_snapshot(
            order_groups,
            operational_day=operational_day,
            sheet_id=self.worksheet.id,
            spreadsheet_id=getattr(self.spreadsheet, "id", ""),
            daily_usd_rates=daily_usd_rates,
        )
        rows = snapshot.rows
        last_used_row = snapshot.last_used_row

        latest_values = self.worksheet.get_all_values(value_render_option="FORMULA")
        if latest_values != existing_values:
            raise ConcurrentSheetEditError(
                "Google Sheet changed during synchronization; rebuild was cancelled "
                "to preserve the newer manual edits"
            )

        if last_used_row > self.worksheet.row_count:
            self.worksheet.add_rows(last_used_row - self.worksheet.row_count)
        self.spreadsheet.batch_update(
            {
                "requests": [
                    {
                        "unmergeCells": {
                            "range": {
                                "sheetId": self.worksheet.id,
                                "startRowIndex": 0,
                                "endRowIndex": self.worksheet.row_count,
                                "startColumnIndex": 0,
                                "endColumnIndex": LAST_COLUMN,
                            }
                        }
                    }
                ]
            }
        )
        self.worksheet.update(
            values=rows,
            range_name=f"A1:{LAST_COLUMN_LETTER}{last_used_row}",
            raw=False,
        )
        if last_used_row < self.worksheet.row_count:
            self.worksheet.batch_clear(
                [f"A{last_used_row + 1}:{LAST_COLUMN_LETTER}{self.worksheet.row_count}"]
            )
        if snapshot.merge_requests:
            self.spreadsheet.batch_update({"requests": snapshot.merge_requests})
        self._apply_professional_formatting(last_used_row)
        self._refresh_end_navigation_link(last_used_row)
        return order_groups.added_rows

    def requires_schema_migration(self) -> bool:
        """Return whether business, receipt, commission, or provenance schema is outdated."""
        self._locate_header_row()
        headers = self.worksheet.row_values(self.header_row)
        net_profit_header = (
            str(headers[COLUMNS.net_profit - 1]).strip().casefold()
            if len(headers) >= COLUMNS.net_profit
            else ""
        )
        expected_net_profit = BUSINESS_HEADERS[COLUMNS.net_profit - 1].casefold()
        current = (
            str(headers[COLUMNS.receipt - 1]).strip().casefold()
            if len(headers) >= COLUMNS.receipt
            else ""
        )
        migrated_header = (
            str(headers[COLUMNS.installment_commission - 1]).strip().casefold()
            if len(headers) >= COLUMNS.installment_commission
            else ""
        )
        source_header = (
            str(headers[COLUMNS.installment_commission_source - 1]).strip().casefold()
            if len(headers) >= COLUMNS.installment_commission_source
            else ""
        )
        legacy = LEGACY_INSTALLMENT_HEADER.casefold()
        expected_source = ALL_HEADERS[COLUMNS.installment_commission_source - 1].casefold()
        reporting_header = (
            str(headers[COLUMNS.reporting_state - 1]).strip().casefold()
            if len(headers) >= COLUMNS.reporting_state
            else ""
        )
        expected_reporting = ALL_HEADERS[COLUMNS.reporting_state - 1].casefold()
        supplier_metadata_headers = tuple(
            str(headers[column - 1]).strip().casefold()
            if len(headers) >= column
            else ""
            for column in (
                COLUMNS.supplier_cost_source,
                COLUMNS.supplier_cost_currency,
                COLUMNS.supplier_cost_original,
            )
        )
        expected_supplier_metadata_headers = tuple(
            ALL_HEADERS[column - 1].casefold()
            for column in (
                COLUMNS.supplier_cost_source,
                COLUMNS.supplier_cost_currency,
                COLUMNS.supplier_cost_original,
            )
        )
        return (
            net_profit_header != expected_net_profit
            or current == legacy
            or (not current and migrated_header == legacy)
            or source_header != expected_source
            or reporting_header != expected_reporting
            or supplier_metadata_headers != expected_supplier_metadata_headers
            or self._requires_daily_usd_rate_layout()
        )

    def _requires_daily_usd_rate_layout(self) -> bool:
        """Detect interrupted or not-yet-applied daily FX control migration."""
        rows = self.worksheet.get(
            f"A1:E{self.worksheet.row_count}",
            value_render_option="FORMULA",
        )
        return any(
            str(_cell_value(row, 1)).strip().casefold() == DAY_DATE_LABEL.casefold()
            and str(_cell_value(row, USD_RATE_LABEL_COLUMN)).strip().casefold()
            != USD_RATE_LABEL.casefold()
            for row in rows
        )


def _row_operational_day(row: list[Any]) -> date | None:
    """Return the hidden operational date used to select a manual FX rate."""
    raw = row[COLUMNS.operational_date - 1] if len(row) >= COLUMNS.operational_date else ""
    return parse_sheet_date(raw)


def _cell_value(row: list[Any], column: int) -> Any:
    return row[column - 1] if len(row) >= column else ""


def _normalize_supplier_costs(
    costs: Mapping[SupplierCostKey, ResolvedSupplierCost],
) -> dict[SupplierCostKey, ResolvedSupplierCost]:
    normalized: dict[SupplierCostKey, ResolvedSupplierCost] = {}
    for key, value in costs.items():
        tracking = tracking_match_key(key.tracking_number)
        if tracking:
            normalized[
                SupplierCostKey(tracking, product_code_match_key(key.product_code))
            ] = value
    return normalized


def _with_melad_provenance_fallbacks(
    rows: list[list[Any]],
    costs: Mapping[SupplierCostKey, ResolvedSupplierCost],
) -> dict[SupplierCostKey, ResolvedSupplierCost]:
    """Use stored Melad USD provenance when an old supplier row was archived."""
    resolved = dict(costs)
    for row in rows:
        if str(_cell_value(row, COLUMNS.row_type)).strip() != ROW_ORDER:
            continue
        if (
            str(_cell_value(row, COLUMNS.supplier_cost_source)).strip()
            != MELAD_SUPPLIER_SOURCE
            or str(_cell_value(row, COLUMNS.supplier_cost_currency)).strip().upper()
            != "USD"
        ):
            continue
        tracking = tracking_match_key(_cell_value(row, COLUMNS.tracking_number))
        product_code = product_code_match_key(_cell_value(row, COLUMNS.product_code))
        if not tracking:
            continue
        key = SupplierCostKey(tracking, product_code)
        if key in resolved or SupplierCostKey(tracking) in resolved:
            continue
        original_cost = decimal_value(
            _cell_value(row, COLUMNS.supplier_cost_original), Decimal("-1")
        )
        if original_cost < 0:
            continue
        resolved[key] = ResolvedSupplierCost(
            source=MELAD_SUPPLIER_SOURCE,
            record=SupplierCostRecord.cost(original_cost, currency="USD"),
            sender=MELAD_SENDER,
        )
    return resolved


def _discover_supplier_cost_candidates(
    rows: list[list[Any]],
    costs: Mapping[SupplierCostKey, ResolvedSupplierCost],
) -> tuple[list[_SupplierCostCandidate], list[str]]:
    raw_rates, valid_rates, _ = _daily_usd_rates(rows)
    candidates: list[_SupplierCostCandidate] = []
    warnings: list[str] = []
    warned_days: set[date] = set()
    warned_missing_day = False
    warned_ambiguous_tracking: set[str] = set()
    tracking_row_counts: dict[str, int] = {}
    for row in rows:
        if str(_cell_value(row, COLUMNS.row_type)).strip() != ROW_ORDER:
            continue
        tracking = tracking_match_key(_cell_value(row, COLUMNS.tracking_number))
        if tracking:
            tracking_row_counts[tracking] = tracking_row_counts.get(tracking, 0) + 1
    for row_number, row in enumerate(rows, start=1):
        if str(_cell_value(row, COLUMNS.row_type)).strip() != ROW_ORDER:
            continue
        tracking = tracking_match_key(_cell_value(row, COLUMNS.tracking_number))
        product_code = product_code_match_key(_cell_value(row, COLUMNS.product_code))
        key = SupplierCostKey(tracking, product_code)
        expected = costs.get(key)
        if expected is None:
            expected = costs.get(SupplierCostKey(tracking))
            if expected is not None and tracking_row_counts.get(tracking, 0) != 1:
                if tracking not in warned_ambiguous_tracking:
                    warnings.append(
                        f"Supplier cost for TTN {tracking} was skipped: product code is "
                        "missing and the CRM order has multiple item rows"
                    )
                    warned_ambiguous_tracking.add(tracking)
                continue
        if expected is None:
            continue
        current_cost = _cell_value(row, COLUMNS.cost)
        current_source = str(
            _cell_value(row, COLUMNS.supplier_cost_source)
        ).strip()
        can_refresh = (
            expected.record.currency == "USD" and current_source == expected.source
        )
        if str(current_cost).strip() and not can_refresh:
            continue
        operational_day = _row_operational_day(row)
        rate = valid_rates.get(operational_day) if operational_day else None
        if expected.record.currency == "USD" and rate is None:
            if operational_day and operational_day not in warned_days:
                raw_rate = raw_rates.get(operational_day, "")
                reason = (
                    "is empty"
                    if str(raw_rate).strip() == ""
                    else f"is invalid ({raw_rate!r})"
                )
                warnings.append(
                    f"{expected.sender or expected.source} costs for "
                    f"{operational_day:%d.%m.%Y} were skipped: daily USD rate {reason}"
                )
                warned_days.add(operational_day)
            elif operational_day is None and not warned_missing_day:
                warnings.append(
                    f"{expected.sender or expected.source} cost was skipped: "
                    "CRM order row has no operational date"
                )
                warned_missing_day = True
            continue
        candidates.append(
            _SupplierCostCandidate(
                row_number=row_number,
                original_row=row,
                key=key,
                expected=expected,
                operational_day=operational_day,
                rate=rate,
            )
        )
    return candidates, warnings


def _supplier_sheet_value(
    expected: ResolvedSupplierCost, rate: Decimal | None
) -> str | int | float:
    record = expected.record
    if record.kind == "unit_cost":
        if record.unit_cost is None:
            raise ValueError("unit cost record is missing its numeric value")
        numeric_cost = record.unit_cost
        if record.currency == "USD":
            if rate is None:
                raise ValueError("USD supplier cost is missing its daily rate")
            numeric_cost = (numeric_cost * rate).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        return decimal_for_sheet(numeric_cost)
    if record.kind == "prepayment":
        return "предоплата"
    if record.text_value is None:
        raise ValueError("text cost record is missing its text value")
    return record.text_value


def _supplier_cost_values_equal(
    current: Any,
    expected_value: str | int | float,
    expected: ResolvedSupplierCost,
) -> bool:
    if expected.record.kind == "unit_cost":
        return decimal_value(current) == decimal_value(expected_value)
    return str(current).strip().casefold() == str(expected_value).strip().casefold()


def _append_supplier_cost_cell_updates(
    primary_updates: list[dict[str, Any]],
    formula_updates: list[dict[str, Any]],
    *,
    row_number: int,
    expected: ResolvedSupplierCost,
    sheet_value: str | int | float,
) -> None:
    cost_cell = rowcol_to_a1(row_number, COLUMNS.cost)
    primary_updates.append({"range": cost_cell, "values": [[sheet_value]]})
    formula_updates.append(
        {
            "range": rowcol_to_a1(row_number, COLUMNS.markup),
            "values": [[markup_formula(row_number)]],
        }
    )
    formula_updates.append(
        {
            "range": rowcol_to_a1(row_number, COLUMNS.net_profit),
            "values": [[net_profit_formula(row_number)]],
        }
    )
    original_value = (
        decimal_for_sheet(expected.record.unit_cost)
        if expected.record.unit_cost is not None
        else ""
    )
    primary_updates.append(
        {
            "range": (
                f"{rowcol_to_a1(row_number, COLUMNS.supplier_cost_source)}:"
                f"{rowcol_to_a1(row_number, COLUMNS.supplier_cost_original)}"
            ),
            "values": [
                [expected.source, expected.record.currency, original_value]
            ],
        }
    )


def _parse_usd_rate(value: Any) -> Decimal | None:
    """Parse a plausible manual UAH/USD rate in the documented ``xx,xx`` form."""
    raw = str(value or "").strip().replace("\u00a0", "").replace(" ", "")
    if not raw or _USD_RATE_PATTERN.fullmatch(raw) is None:
        return None
    try:
        rate = Decimal(raw.replace(",", "."))
    except InvalidOperation:
        return None
    return (
        rate
        if rate.is_finite() and _MIN_USD_RATE <= rate <= _MAX_USD_RATE
        else None
    )


def _daily_usd_rates(
    rows: list[list[Any]],
) -> tuple[dict[date, Any], dict[date, Decimal], dict[date, int]]:
    raw_rates: dict[date, Any] = {}
    valid_rates: dict[date, Decimal] = {}
    rate_rows: dict[date, int] = {}
    for row_number, row in enumerate(rows, start=1):
        row_type = str(row[COLUMNS.row_type - 1]).strip() if len(row) >= COLUMNS.row_type else ""
        if row_type != ROW_DAY:
            continue
        day = _row_operational_day(row) or parse_sheet_date(row[1] if len(row) > 1 else "")
        if day is None:
            continue
        raw = row[USD_RATE_VALUE_COLUMN - 1] if len(row) >= USD_RATE_VALUE_COLUMN else ""
        if day in raw_rates:
            raw_rates[day] = "duplicate day rows"
            valid_rates.pop(day, None)
            rate_rows.pop(day, None)
            continue
        raw_rates[day] = raw
        rate_rows[day] = row_number
        if (rate := _parse_usd_rate(raw)) is not None:
            valid_rates[day] = rate
    return raw_rates, valid_rates, rate_rows


def _extract_daily_usd_rate_values(rows: list[list[Any]]) -> dict[date, Any]:
    """Preserve user-entered daily rates during structural sheet rebuilds."""
    raw_rates, _, _ = _daily_usd_rates(rows)
    return {
        day: "" if value == "duplicate day rows" else value
        for day, value in raw_rates.items()
    }
