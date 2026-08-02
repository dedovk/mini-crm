from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

from crm_sync.models import Order, ShipmentStatus
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
    clean_customer_display,
    month_period_label,
    parse_sheet_date,
    report_formulas,
    sheet_serial,
)
from crm_sync.utils import decimal_for_sheet, extract_ttn, parse_prepayment

LOGGER = logging.getLogger(__name__)

PAYMENT_OPTIONS = (
    "пром оплата(оплата картой)",
    "оплата частями",
    "наложка",
    "оплата на счет",
    "смешанная",
)

NOVA_POSHTA_STATUS_OPTIONS = (
    "Створено електронну накладну",
    "Нова Пошта очікує надходження",
    "Прийнято у відділенні",
    "Відправлення у дорозі",
    "Прибуло у відділення",
    "Передано кур'єру",
    "Отримано",
    "Відмова від отримання",
    "Повертається відправнику",
    "Повернуто відправнику",
    "Невідомо",
)


@dataclass(frozen=True, slots=True)
class SheetColumns:
    source: int = 1
    tracking_number: int = 2
    shipment_status: int = 3
    order_date: int = 4
    order_number: int = 5
    customer: int = 6
    phone: int = 7
    product: int = 8
    sku: int = 9
    sender: int = 10
    quantity: int = 11
    unit_price: int = 12
    line_total: int = 13
    order_total: int = 14
    payment_method: int = 15
    prepayment: int = 16
    cost: int = 17
    markup: int = 18
    advertising: int = 19
    manager_note: int = 20
    sync_key: int = 21
    row_type: int = 22
    operational_date: int = 23


COLUMNS = SheetColumns()


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

    def ensure_schema(self, *, apply_changes: bool = True) -> None:
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
            if self.worksheet.col_count < COLUMNS.operational_date:
                self.worksheet.add_cols(COLUMNS.operational_date - self.worksheet.col_count)
            self.worksheet.update(
                values=[list(ALL_HEADERS[COLUMNS.sync_key - 1 :])],
                range_name=(
                    f"{rowcol_to_a1(self.header_row, COLUMNS.sync_key)}:"
                    f"{rowcol_to_a1(self.header_row, COLUMNS.operational_date)}"
                ),
                raw=True,
            )
            self._configure_validations_and_hidden_key()

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
                        "endIndex": COLUMNS.operational_date,
                    },
                    "properties": {"hiddenByUser": True},
                    "fields": "hiddenByUser",
                }
            },
        ]
        self.spreadsheet.batch_update({"requests": requests})

    def prepare_daily_layout(self, current_day: date) -> None:
        values = self.worksheet.get(
            f"A1:W{self.worksheet.row_count}",
            value_render_option="UNFORMATTED_VALUE",
        )
        if not values:
            raise RuntimeError("Google Sheet is empty; the expected CRM template was not found")

        day_rows: list[tuple[int, date]] = []
        header_rows: set[int] = set()
        active_day: date | None = None
        value_updates: list[dict[str, Any]] = []
        row_types: dict[int, str] = {}
        closed_days: set[date] = set()

        for row_number, row in enumerate(values, start=1):
            row_type = str(row[COLUMNS.row_type - 1]).strip() if len(row) >= COLUMNS.row_type else ""
            marker = str(row[0]).strip().casefold() if row else ""
            if row_type == ROW_DAY or marker == "дата дня":
                parsed = parse_sheet_date(row[1] if len(row) > 1 else "")
                if parsed:
                    active_day = parsed
                    day_rows.append((row_number, parsed))
                    row_types[row_number] = ROW_DAY
                    stored_day = parse_sheet_date(
                        row[COLUMNS.operational_date - 1] if len(row) >= COLUMNS.operational_date else ""
                    )
                    if row_type != ROW_DAY or stored_day != parsed:
                        value_updates.extend(self._technical_cell_updates(row_number, ROW_DAY, parsed))
                    header_row = row_number + 1
                    header_rows.add(header_row)
                    row_types[header_row] = ROW_HEADER
                    existing_header = values[header_row - 1] if header_row <= len(values) else []
                    if list(existing_header[: COLUMNS.operational_date]) != list(ALL_HEADERS):
                        value_updates.append(
                            {"range": f"A{header_row}:W{header_row}", "values": [list(ALL_HEADERS)]}
                        )
                continue
            if row_number in header_rows or (
                marker == BUSINESS_HEADERS[0].casefold()
                and len(row) > 1
                and str(row[1]).strip().casefold() == BUSINESS_HEADERS[1].casefold()
            ):
                row_types[row_number] = ROW_HEADER
                continue
            if row_type in {ROW_REPORT_DAY, ROW_REPORT_MTD, ROW_REPORT_FORECAST}:
                report_day = parse_sheet_date(row[COLUMNS.operational_date - 1] if len(row) >= COLUMNS.operational_date else "")
                if row_type == ROW_REPORT_DAY and report_day:
                    closed_days.add(report_day)
                row_types[row_number] = row_type
                continue
            if row_type == ROW_MONTH or marker == "місяць":
                row_types[row_number] = ROW_MONTH
                continue

            sync_key = str(row[COLUMNS.sync_key - 1]).strip() if len(row) >= COLUMNS.sync_key else ""
            source = str(row[COLUMNS.source - 1]).strip() if len(row) >= COLUMNS.source else ""
            order_id = str(row[COLUMNS.order_number - 1]).strip() if len(row) >= COLUMNS.order_number else ""
            if active_day and ((sync_key and sync_key.casefold() != "sync key") or (source and order_id)):
                row_types[row_number] = ROW_ORDER
                stored_day = parse_sheet_date(
                    row[COLUMNS.operational_date - 1] if len(row) >= COLUMNS.operational_date else ""
                )
                if row_type != ROW_ORDER or stored_day != active_day:
                    value_updates.extend(self._technical_cell_updates(row_number, ROW_ORDER, active_day))
                if len(row) >= COLUMNS.customer:
                    cleaned = clean_customer_display(row[COLUMNS.customer - 1])
                    if cleaned != str(row[COLUMNS.customer - 1]).strip():
                        value_updates.append({"range": f"F{row_number}", "values": [[cleaned]]})

        if not day_rows:
            raise RuntimeError("No 'Дата дня' row was found in the CRM worksheet")

        first_day = day_rows[0][1]
        first_month = first_day.replace(day=1)
        first_row = values[0] if values else []
        first_row_month = parse_sheet_date(
            first_row[COLUMNS.operational_date - 1] if len(first_row) >= COLUMNS.operational_date else ""
        )
        if (
            len(first_row) < 2
            or str(first_row[0]).strip() != "Місяць"
            or str(first_row[1]).strip() != month_period_label(first_day)
            or (str(first_row[COLUMNS.row_type - 1]).strip() if len(first_row) >= COLUMNS.row_type else "") != ROW_MONTH
            or first_row_month != first_month
        ):
            value_updates.extend(
                [
                    {"range": "A1:B1", "values": [["Місяць", month_period_label(first_day)]]},
                    *self._technical_cell_updates(1, ROW_MONTH, first_month),
                ]
            )
        row_types[1] = ROW_MONTH

        last_used_row = max(
            (row_number for row_number, row in enumerate(values, start=1) if any(str(value).strip() for value in row)),
            default=self.header_row,
        )
        latest_day = day_rows[-1][1]
        if latest_day > current_day:
            raise RuntimeError(f"Latest sheet day {latest_day} is later than current day {current_day}")

        while latest_day < current_day:
            if latest_day not in closed_days:
                formulas = report_formulas(
                    latest_day,
                    first_data_row=self.header_row + 1,
                    last_data_row=last_used_row,
                )
                labels = {
                    ROW_REPORT_DAY: f"Підсумок за {latest_day:%d.%m.%Y}",
                    ROW_REPORT_MTD: f"Разом за {latest_day.day} дн. місяця",
                    ROW_REPORT_FORECAST: "Прогноз на місяць",
                }
                for row_type in (ROW_REPORT_DAY, ROW_REPORT_MTD, ROW_REPORT_FORECAST):
                    last_used_row += 1
                    report_row: list[Any] = [""] * COLUMNS.operational_date
                    report_row[0] = labels[row_type]
                    for column, formula in formulas[row_type].items():
                        report_row[column - 1] = formula
                    report_row[COLUMNS.row_type - 1] = row_type
                    report_row[COLUMNS.operational_date - 1] = sheet_serial(latest_day)
                    value_updates.append(
                        {"range": f"A{last_used_row}:W{last_used_row}", "values": [report_row]}
                    )
                    row_types[last_used_row] = row_type
                closed_days.add(latest_day)

            next_day = latest_day + timedelta(days=1)
            last_used_row += 2  # one visual separator row
            if next_day.month != latest_day.month:
                value_updates.append(
                    {
                        "range": f"A{last_used_row}:W{last_used_row}",
                        "values": [["Місяць", month_period_label(next_day)] + [""] * 19 + [ROW_MONTH, sheet_serial(next_day.replace(day=1))]],
                    }
                )
                row_types[last_used_row] = ROW_MONTH
                last_used_row += 2

            day_row = last_used_row
            header_row = day_row + 1
            day_values: list[Any] = [""] * COLUMNS.operational_date
            day_values[0] = "Дата дня"
            day_values[1] = sheet_serial(next_day)
            day_values[COLUMNS.row_type - 1] = ROW_DAY
            day_values[COLUMNS.operational_date - 1] = sheet_serial(next_day)
            value_updates.extend(
                [
                    {"range": f"A{day_row}:W{day_row}", "values": [day_values]},
                    {"range": f"A{header_row}:W{header_row}", "values": [list(ALL_HEADERS)]},
                ]
            )
            row_types[day_row] = ROW_DAY
            row_types[header_row] = ROW_HEADER
            latest_day = next_day
            last_used_row = header_row

        if last_used_row > self.worksheet.row_count:
            self.worksheet.add_rows(last_used_row - self.worksheet.row_count)
        if value_updates:
            self.worksheet.batch_update(value_updates, raw=False)
            self._apply_professional_formatting(last_used_row)

    @staticmethod
    def _technical_cell_updates(row_number: int, row_type: str, operational_day: date) -> list[dict[str, Any]]:
        return [
            {"range": f"V{row_number}:W{row_number}", "values": [[row_type, sheet_serial(operational_day)]]}
        ]

    def _apply_professional_formatting(self, last_used_row: int) -> None:
        values = self.worksheet.get(f"A1:W{last_used_row}", value_render_option="UNFORMATTED_VALUE")
        typed_rows: dict[str, list[int]] = {}
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
                        "endColumnIndex": COLUMNS.operational_date,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {"fontFamily": "Arial", "fontSize": 10},
                            "horizontalAlignment": "CENTER",
                            "verticalAlignment": "MIDDLE",
                            "wrapStrategy": "WRAP",
                            "borders": {
                                "bottom": {"style": "SOLID", "color": {"red": 0.85, "green": 0.88, "blue": 0.91}}
                            },
                        }
                    },
                    "fields": "userEnteredFormat(textFormat,horizontalAlignment,verticalAlignment,wrapStrategy,borders.bottom)",
                }
            },
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": COLUMNS.sync_key - 1,
                        "endIndex": COLUMNS.operational_date,
                    },
                    "properties": {"hiddenByUser": True},
                    "fields": "hiddenByUser",
                }
            },
            {
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": 0,
                        "endIndex": last_used_row,
                    }
                }
            },
        ]

        widths = (105, 140, 210, 145, 125, 210, 145, 250, 125, 125, 90, 135, 155, 145, 190, 125, 155, 130, 145, 240)
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
            ROW_MONTH: ("#17365D", "#FFFFFF", 15, 52),
            ROW_DAY: ("#D9EAF7", "#17365D", 12, 32),
            ROW_HEADER: ("#2F75B5", "#FFFFFF", 10, 58),
            ROW_REPORT_DAY: ("#E2F0D9", "#375623", 10, 32),
            ROW_REPORT_MTD: ("#DDEBF7", "#1F4E78", 10, 32),
            ROW_REPORT_FORECAST: ("#FCE4D6", "#9C5700", 10, 32),
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
                                    "endColumnIndex": COLUMNS.operational_date,
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
                if row_type == ROW_DAY:
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

        for row_number in typed_rows.get(ROW_ORDER, []):
            if row_number % 2:
                continue
            requests.append(
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
                                "backgroundColorStyle": {"rgbColor": self._hex_color("#F3F8FC")}
                            }
                        },
                        "fields": "userEnteredFormat.backgroundColorStyle",
                    }
                }
            )

        number_formats = {
            COLUMNS.tracking_number: ("TEXT", "@"),
            COLUMNS.order_date: ("DATE_TIME", "dd.mm.yyyy hh:mm"),
            COLUMNS.order_number: ("TEXT", "@"),
            COLUMNS.phone: ("TEXT", "@"),
            COLUMNS.sku: ("TEXT", "@"),
            COLUMNS.quantity: ("NUMBER", "0"),
            COLUMNS.unit_price: ("NUMBER", "#,##0.00"),
            COLUMNS.line_total: ("NUMBER", "#,##0.00"),
            COLUMNS.order_total: ("NUMBER", "#,##0.00"),
            COLUMNS.prepayment: ("NUMBER", "#,##0.00"),
            COLUMNS.cost: ("NUMBER", "#,##0.00"),
            COLUMNS.markup: ("NUMBER", "#,##0.00"),
            COLUMNS.advertising: ("NUMBER", "#,##0.00"),
            COLUMNS.operational_date: ("DATE", "dd.mm.yyyy"),
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
        for row in values[self.header_row :]:
            row_type = row[COLUMNS.row_type - 1].strip() if len(row) >= COLUMNS.row_type else ""
            if row_type and row_type != ROW_ORDER:
                continue
            if len(row) >= COLUMNS.sync_key and row[COLUMNS.sync_key - 1].strip():
                key = row[COLUMNS.sync_key - 1].strip().casefold()
                if key != "sync key":
                    keys.add(key)
                continue
            source = row[COLUMNS.source - 1].strip() if len(row) >= COLUMNS.source else ""
            order_id = row[COLUMNS.order_number - 1].strip() if len(row) >= COLUMNS.order_number else ""
            if source and order_id:
                keys.add(f"{source.casefold()}:{order_id}")
        return keys

    def pending_tracking_numbers(self) -> list[str]:
        values = self.worksheet.get_all_values()
        numbers: list[str] = []
        for row in values[self.header_row :]:
            raw_tracking = row[COLUMNS.tracking_number - 1].strip() if len(row) >= COLUMNS.tracking_number else ""
            tracking = extract_ttn(raw_tracking)
            status = row[COLUMNS.shipment_status - 1].strip().casefold() if len(row) >= COLUMNS.shipment_status else ""
            if tracking and not self._is_final_status(status):
                numbers.append(tracking)
        return list(dict.fromkeys(numbers))

    @staticmethod
    def _is_final_status(status: str) -> bool:
        return "отриман" in status or "відмова від отримання" in status

    def update_shipment_statuses(self, statuses: dict[str, ShipmentStatus]) -> int:
        if not statuses:
            return 0
        values = self.worksheet.get_all_values()
        updates: list[dict[str, Any]] = []
        for row_number, row in enumerate(values[self.header_row :], start=self.header_row + 1):
            raw_tracking = row[COLUMNS.tracking_number - 1].strip() if len(row) >= COLUMNS.tracking_number else ""
            tracking = extract_ttn(raw_tracking)
            status = statuses.get(tracking)
            if not status:
                continue
            current = row[COLUMNS.shipment_status - 1].strip() if len(row) >= COLUMNS.shipment_status else ""
            if current != status.status:
                updates.append(
                    {
                        "range": rowcol_to_a1(row_number, COLUMNS.shipment_status),
                        "values": [[status.status]],
                    }
                )
        if updates:
            self.worksheet.batch_update(updates, raw=True)
        return len(updates)

    def append_orders(
        self,
        orders: list[Order],
        shipment_statuses: dict[str, ShipmentStatus],
        *,
        sender_default: str,
        operational_day: date,
    ) -> int:
        if not orders:
            return 0
        existing_values = self.worksheet.get_all_values()
        start_row = max(self.header_row + 1, len(existing_values) + 1)
        rows: list[list[Any]] = []
        merge_requests: list[dict[str, Any]] = []
        formula_updates: list[dict[str, Any]] = []
        current_row = start_row

        for order in orders:
            order_start = current_row
            shipment_status = shipment_statuses.get(order.tracking_number)
            prepayment = parse_prepayment(order.note)
            sender = order.sender.strip() or sender_default
            for item in order.items:
                row: list[Any] = [""] * COLUMNS.operational_date
                row[COLUMNS.source - 1] = order.source
                row[COLUMNS.tracking_number - 1] = order.tracking_number
                row[COLUMNS.shipment_status - 1] = shipment_status.status if shipment_status else "Невідомо"
                row[COLUMNS.order_date - 1] = order.created_at.strftime("%d.%m.%Y %H:%M")
                row[COLUMNS.order_number - 1] = order.external_id
                row[COLUMNS.customer - 1] = ", ".join(part for part in (order.city, order.customer_name) if part)
                row[COLUMNS.phone - 1] = order.phone
                row[COLUMNS.product - 1] = item.name
                row[COLUMNS.sku - 1] = item.sku
                row[COLUMNS.sender - 1] = sender
                row[COLUMNS.quantity - 1] = decimal_for_sheet(item.quantity)
                row[COLUMNS.unit_price - 1] = decimal_for_sheet(item.unit_price)
                row[COLUMNS.line_total - 1] = decimal_for_sheet(item.line_total)
                row[COLUMNS.order_total - 1] = decimal_for_sheet(order.total)
                row[COLUMNS.payment_method - 1] = order.payment_method
                row[COLUMNS.prepayment - 1] = decimal_for_sheet(prepayment) if prepayment > Decimal(0) else ""
                row[COLUMNS.sync_key - 1] = order.sync_key
                row[COLUMNS.row_type - 1] = ROW_ORDER
                row[COLUMNS.operational_date - 1] = sheet_serial(operational_day)
                rows.append(row)
                formula_updates.append(
                    {
                        "range": rowcol_to_a1(current_row, COLUMNS.markup),
                        "values": [[f"=(L{current_row}-Q{current_row})*K{current_row}"]],
                    }
                )
                current_row += 1

            if current_row - order_start > 1:
                for column in (COLUMNS.order_number, COLUMNS.order_total):
                    merge_requests.append(
                        {
                            "mergeCells": {
                                "range": {
                                    "sheetId": self.worksheet.id,
                                    "startRowIndex": order_start - 1,
                                    "endRowIndex": current_row - 1,
                                    "startColumnIndex": column - 1,
                                    "endColumnIndex": column,
                                },
                                "mergeType": "MERGE_ALL",
                            }
                        }
                    )

        end_row = start_row + len(rows) - 1
        if end_row > self.worksheet.row_count:
            self.worksheet.add_rows(end_row - self.worksheet.row_count)
        self.worksheet.update(
            values=rows,
            range_name=f"A{start_row}:W{end_row}",
            raw=True,
        )
        if formula_updates:
            self.worksheet.batch_update(formula_updates, raw=False)
        if merge_requests:
            self.spreadsheet.batch_update({"requests": merge_requests})
        self._apply_professional_formatting(end_row)
        return len(rows)
