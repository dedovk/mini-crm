from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

from crm_sync.models import Order, ShipmentStatus
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
            COLUMNS.source: "джерело",
            COLUMNS.tracking_number: "ттн",
            COLUMNS.order_number: "номер замовлення",
            COLUMNS.product: "товар",
            COLUMNS.order_total: "загальна сума замовлення",
        }
        for column, signal in required_signals.items():
            value = headers[column - 1].casefold() if len(headers) >= column else ""
            if signal not in value:
                raise RuntimeError(
                    f"Google Sheets schema mismatch at {rowcol_to_a1(self.header_row, column)}: "
                    f"expected header containing {signal!r}"
                )

        if apply_changes:
            sync_cell = rowcol_to_a1(self.header_row, COLUMNS.sync_key)
            if len(headers) < COLUMNS.sync_key or "sync key" not in headers[COLUMNS.sync_key - 1].casefold():
                self.worksheet.update_acell(sync_cell, "Sync Key")
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
                        "endIndex": COLUMNS.sync_key,
                    },
                    "properties": {"hiddenByUser": True},
                    "fields": "hiddenByUser",
                }
            },
        ]
        self.spreadsheet.batch_update({"requests": requests})

    def read_existing_sync_keys(self) -> set[str]:
        values = self.worksheet.get_all_values()
        keys: set[str] = set()
        for row in values[self.header_row :]:
            if len(row) >= COLUMNS.sync_key and row[COLUMNS.sync_key - 1].strip():
                keys.add(row[COLUMNS.sync_key - 1].strip().casefold())
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
                row: list[Any] = [""] * COLUMNS.sync_key
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
            range_name=f"A{start_row}:U{end_row}",
            raw=True,
        )
        if formula_updates:
            self.worksheet.batch_update(formula_updates, raw=False)
        if merge_requests:
            self.spreadsheet.batch_update({"requests": merge_requests})
        return len(rows)
