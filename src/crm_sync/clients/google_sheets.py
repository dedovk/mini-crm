from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import WorksheetNotFound
from gspread.utils import rowcol_to_a1

from crm_sync.integrity import IntegrityReport
from crm_sync.models import (
    Order,
    OrderAuditEvent,
    ShipmentStatus,
    ShipmentStatusChange,
    ShipmentUpdateResult,
    SyncHealthState,
)
from crm_sync.sheet_layout import (
    ALL_HEADERS,
    BUSINESS_HEADERS,
    ROW_DAY,
    ROW_HEADER,
    ROW_MONTH,
    ROW_ORDER,
    ROW_REPORT_DAY,
    ROW_REPORT_FORECAST,
    ROW_REPORT_MTD,
    parse_sheet_date,
    sheet_serial,
    source_display,
    source_key,
)
from crm_sync.sheet_orders import collect_order_groups
from crm_sync.sheet_schema import (
    AUDIT_HEADERS,
    AUDIT_WORKSHEET_NAME,
    BACKUP_PREFIX,
    BACKUP_RETENTION,
    COLUMNS,
    HEALTH_ALERT_THRESHOLD,
    HEALTH_WORKSHEET_NAME,
    LAST_COLUMN,
    LAST_COLUMN_LETTER,
    NOVA_POSHTA_STATUS_OPTIONS,
    PAYMENT_OPTIONS,
)
from crm_sync.sheet_snapshot import build_sheet_snapshot
from crm_sync.utils import (
    customer_display,
    decimal_for_sheet,
    decimal_value,
    extract_ttn,
)

LOGGER = logging.getLogger(__name__)

class GoogleSheetsGateway:
    def __init__(
        self,
        *,
        credentials_info: dict[str, Any],
        spreadsheet_id: str,
        worksheet_name: str,
        header_row: int,
        sender_options: tuple[str, ...],
    ) -> None:
        credentials = Credentials.from_service_account_info(
            credentials_info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        client = gspread.authorize(credentials)
        self.spreadsheet = client.open_by_key(spreadsheet_id)
        self.worksheet = self.spreadsheet.worksheet(worksheet_name)
        self.header_row = header_row
        self.sender_options = sender_options

    def _ensure_audit_worksheet(self):
        created = False
        try:
            audit = self.spreadsheet.worksheet(AUDIT_WORKSHEET_NAME)
        except WorksheetNotFound:
            created = True
            audit = self.spreadsheet.add_worksheet(
                title=AUDIT_WORKSHEET_NAME,
                rows=1000,
                cols=len(AUDIT_HEADERS),
            )
        header_changed = audit.row_values(1)[: len(AUDIT_HEADERS)] != list(AUDIT_HEADERS)
        if header_changed:
            audit.update(values=[list(AUDIT_HEADERS)], range_name="A1:J1", raw=True)
        if created or header_changed:
            audit.freeze(rows=1)
            audit.format(
                "A1:J1",
                {
                    "backgroundColor": {"red": 0.12, "green": 0.31, "blue": 0.47},
                    "textFormat": {
                        "bold": True,
                        "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                    },
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                },
            )
        return audit

    def append_audit_events(self, events: list[OrderAuditEvent]) -> int:
        if not events:
            return 0
        audit = self._ensure_audit_worksheet()
        rows = [
            [
                event.occurred_at.strftime("%d.%m.%Y %H:%M:%S"),
                event.event_type,
                source_display(event.source),
                event.order_id,
                event.sync_key,
                event.tracking_number,
                event.field,
                event.old_value,
                event.new_value,
                event.details,
            ]
            for event in events
        ]
        audit.append_rows(rows, value_input_option="USER_ENTERED")
        return len(rows)

    def create_backup(self, *, created_at: datetime) -> str:
        source_title = str(getattr(self.worksheet, "title", "CRM"))
        title = f"{BACKUP_PREFIX}{created_at:%Y%m%d-%H%M%S} - {source_title}"[:100]
        backup = self.spreadsheet.duplicate_sheet(
            source_sheet_id=self.worksheet.id,
            new_sheet_name=title,
        )
        self.spreadsheet.batch_update(
            {
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": backup.id, "hidden": True},
                            "fields": "hidden",
                        }
                    }
                ]
            }
        )
        backups = sorted(
            (
                worksheet
                for worksheet in self.spreadsheet.worksheets()
                if str(getattr(worksheet, "title", "")).startswith(BACKUP_PREFIX)
            ),
            key=lambda worksheet: str(getattr(worksheet, "title", "")),
            reverse=True,
        )
        for obsolete in backups[BACKUP_RETENTION:]:
            self.spreadsheet.del_worksheet(obsolete)
        return title

    def record_sync_health(
        self,
        failed_components: list[str],
        *,
        occurred_at: datetime,
    ) -> SyncHealthState:
        created = False
        try:
            health = self.spreadsheet.worksheet(HEALTH_WORKSHEET_NAME)
        except WorksheetNotFound:
            created = True
            health = self.spreadsheet.add_worksheet(
                title=HEALTH_WORKSHEET_NAME,
                rows=10,
                cols=2,
            )
        current = {
            str(row[0]).strip(): str(row[1]).strip()
            for row in health.get_all_values()
            if len(row) >= 2 and str(row[0]).strip()
        }
        try:
            previous_failures = max(0, int(current.get("consecutive_failures", "0")))
        except ValueError:
            previous_failures = 0
        previous_alert_open = current.get("alert_open", "false").casefold() == "true"
        components = tuple(dict.fromkeys(component for component in failed_components if component))

        if components:
            consecutive = previous_failures + 1
            alert_due = consecutive == HEALTH_ALERT_THRESHOLD and not previous_alert_open
            recovered = False
            alert_open = previous_alert_open or consecutive >= HEALTH_ALERT_THRESHOLD
            outcome = "failed"
        else:
            consecutive = 0
            alert_due = False
            recovered = previous_alert_open or previous_failures >= HEALTH_ALERT_THRESHOLD
            alert_open = False
            outcome = "recovered" if recovered else "ok"

        rows = [
            ["Параметр", "Значення"],
            ["consecutive_failures", str(consecutive)],
            ["alert_open", str(alert_open).lower()],
            ["last_outcome", outcome],
            ["failed_components", ", ".join(components)],
            ["updated_at", occurred_at.isoformat()],
        ]
        health.update(values=rows, range_name="A1:B6", raw=True)
        if created:
            health.freeze(rows=1)
            health.format(
                "A1:B1",
                {
                    "backgroundColor": {"red": 0.12, "green": 0.31, "blue": 0.47},
                    "textFormat": {
                        "bold": True,
                        "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                    },
                },
            )
            self.spreadsheet.batch_update(
                {
                    "requests": [
                        {
                            "updateSheetProperties": {
                                "properties": {"sheetId": health.id, "hidden": True},
                                "fields": "hidden",
                            }
                        }
                    ]
                }
            )
        return SyncHealthState(
            consecutive_failures=consecutive,
            alert_due=alert_due,
            recovered=recovered,
            failed_components=components,
        )

    def ensure_schema(self, *, apply_changes: bool = True) -> None:
        preview = self.worksheet.get(
            f"A1:{LAST_COLUMN_LETTER}{min(self.worksheet.row_count, 100)}",
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
            self._ensure_audit_worksheet()

    def validate_integrity(self) -> IntegrityReport:
        values = self.worksheet.get_all_values(value_render_option="FORMATTED_VALUE")
        errors: list[str] = []
        warnings: list[str] = []
        formula_errors = ("#REF!", "#VALUE!", "#DIV/0!", "#NAME?", "#N/A")
        rows_by_key: dict[str, list[int]] = {}
        totals_by_key: dict[str, set[Decimal]] = {}
        missing_completion_keys: set[str] = set()

        for row_number, row in enumerate(values, start=1):
            for column, value in enumerate(row, start=1):
                rendered = str(value).strip()
                if any(error in rendered for error in formula_errors):
                    errors.append(f"formula error at {rowcol_to_a1(row_number, column)}: {rendered}")
            row_type = str(row[COLUMNS.row_type - 1]).strip() if len(row) >= COLUMNS.row_type else ""
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

        widths = (55, 105, 100, 78, 80, 135, 95, 180, 70, 85, 60, 80, 85, 90, 90, 75, 85, 75, 80, 110)
        for index, width in enumerate(widths):
            requests.append(
                {
                    "updateDimensionProperties": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": index,
                            "endIndex": index + 1,
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
            requests.append(
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
                }
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
                for column in (4, 6, 8, 10, 12):
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

        return [
            rule("Відмова", "#F4CCCC"),
            rule("Повернуто", "#F4CCCC"),
            rule("Відправлення отримано", "#D9EAD3"),
            rule("Отримано", "#D9EAD3"),
            rule("дорозі", "#FFF2CC"),
            rule("Прибуло", "#FFF2CC"),
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
        values = self.worksheet.get(
            f"V1:W{self.worksheet.row_count}",
            value_render_option="UNFORMATTED_VALUE",
        )
        days = [
            parsed
            for row in values
            if row and str(row[0]).strip() == ROW_DAY
            if (parsed := parse_sheet_date(row[1] if len(row) > 1 else "")) is not None
        ]
        return max(days, default=None)

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
                if order and order.tracking_number:
                    current = (
                        str(row[COLUMNS.tracking_number - 1]).strip()
                        if len(row) >= COLUMNS.tracking_number
                        else ""
                    )
                    if current != order.tracking_number:
                        updates.append({"range": f"B{row_number}", "values": [[order.tracking_number]]})
                if order and order.completion_is_exact:
                    completion_date = order.completed_at.date()
                    current_completion_date = parse_sheet_date(
                        row[COLUMNS.order_date - 1] if len(row) >= COLUMNS.order_date else ""
                    )
                    if current_completion_date != completion_date:
                        updates.append(
                            {"range": f"D{row_number}", "values": [[sheet_serial(completion_date)]]}
                        )
                    current_day = parse_sheet_date(
                        row[COLUMNS.operational_date - 1]
                        if len(row) >= COLUMNS.operational_date
                        else ""
                    )
                    if current_day != order.completed_at.date():
                        updates.append(
                            {
                                "range": f"W{row_number}",
                                "values": [[sheet_serial(order.completed_at.date())]],
                            }
                        )
                if customer:
                    current = str(row[COLUMNS.customer - 1]).strip() if len(row) >= COLUMNS.customer else ""
                    if current != customer:
                        updates.append({"range": f"F{row_number}", "values": [[customer]]})
                if order and item_index < len(order.items) and order.items[item_index].product_code:
                    item = order.items[item_index]
                    product_code = item.product_code
                    current = (
                        str(row[COLUMNS.product_code - 1]).strip()
                        if len(row) >= COLUMNS.product_code
                        else ""
                    )
                    if current != product_code:
                        updates.append({"range": f"I{row_number}", "values": [[product_code]]})
                    numeric_updates = {
                        COLUMNS.quantity: item.quantity,
                        COLUMNS.unit_price: item.unit_price,
                        COLUMNS.line_total: item.line_total,
                    }
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
                        updates.append({"range": f"N{row_number}", "values": [[decimal_for_sheet(order.total)]]})
                    current_advertising = row[COLUMNS.advertising - 1] if len(row) >= COLUMNS.advertising else ""
                    if order.advertising_cost > 0 and decimal_value(current_advertising) == 0:
                        updates.append(
                            {
                                "range": f"S{row_number}",
                                "values": [[decimal_for_sheet(order.advertising_cost)]],
                            }
                        )
                if order and order.payment_method:
                    current = (
                        str(row[COLUMNS.payment_method - 1]).strip()
                        if len(row) >= COLUMNS.payment_method
                        else ""
                    )
                    if current != order.payment_method:
                        updates.append({"range": f"O{row_number}", "values": [[order.payment_method]]})
                formula = f"=(L{row_number}-Q{row_number})*K{row_number}"
                current_formula = (
                    str(row[COLUMNS.markup - 1]).strip() if len(row) >= COLUMNS.markup else ""
                )
                if current_formula != formula:
                    updates.append({"range": f"R{row_number}", "values": [[formula]]})

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
            completion_day = order.completed_at.date() if order.completion_is_exact else stored_day
            first_seen = parse_sheet_date(
                row[COLUMNS.first_seen_completed - 1]
                if len(row) >= COLUMNS.first_seen_completed
                else ""
            )
            if first_seen is None:
                first_seen = completion_day or observed_at.date()
                updates.append(
                    {
                        "range": rowcol_to_a1(row_number, COLUMNS.first_seen_completed),
                        "values": [[sheet_serial(first_seen)]],
                    }
                )

            current_completion = parse_sheet_date(
                row[COLUMNS.order_date - 1] if len(row) >= COLUMNS.order_date else ""
            )
            desired_completion = order.completed_at.date() if order.completion_is_exact else first_seen
            if current_completion != desired_completion:
                updates.append(
                    {
                        "range": rowcol_to_a1(row_number, COLUMNS.order_date),
                        "values": [[sheet_serial(desired_completion)]],
                    }
                )

            old_status = (
                str(row[COLUMNS.order_status - 1]).strip()
                if len(row) >= COLUMNS.order_status
                else ""
            )
            if old_status != "Виконано":
                updates.append(
                    {
                        "range": rowcol_to_a1(row_number, COLUMNS.order_status),
                        "values": [["Виконано"]],
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
                            new_value="Виконано",
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
                current = row[COLUMNS.advertising - 1] if len(row) >= COLUMNS.advertising else ""
                if index == 0:
                    if decimal_value(current) != expected:
                        updates.append(
                            {
                                "range": f"S{row_number}",
                                "values": [[decimal_for_sheet(expected)]],
                            }
                        )
                elif str(current).strip():
                    updates.append({"range": f"S{row_number}", "values": [[""]]})
        if updates:
            self.worksheet.batch_update(updates, raw=False)
        return len(updates)

    def append_orders(
        self,
        orders: list[Order],
        shipment_statuses: dict[str, ShipmentStatus],
        *,
        sender_default: str,
        operational_day: date,
        observed_at: datetime | None = None,
        force_rebuild: bool = False,
    ) -> int:
        if not orders and not force_rebuild:
            return 0
        observation_day = (observed_at.date() if observed_at else operational_day)
        existing_values = self.worksheet.get_all_values(value_render_option="FORMULA")
        order_groups = collect_order_groups(
            existing_values,
            orders,
            shipment_statuses,
            sender_default=sender_default,
            observation_day=observation_day,
        )
        snapshot = build_sheet_snapshot(
            order_groups,
            operational_day=operational_day,
            sheet_id=self.worksheet.id,
            spreadsheet_id=getattr(self.spreadsheet, "id", ""),
        )
        rows = snapshot.rows
        last_used_row = snapshot.last_used_row

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
        return order_groups.added_rows
