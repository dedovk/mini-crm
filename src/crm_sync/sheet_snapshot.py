from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from crm_sync.sheet_layout import (
    ALL_HEADERS,
    ROW_DAY,
    ROW_MONTH,
    ROW_REPORT_DAY,
    ROW_REPORT_FORECAST,
    ROW_REPORT_MTD,
    month_period_label,
    report_formulas,
    sheet_serial,
)
from crm_sync.sheet_orders import OrderGroups, markup_formula
from crm_sync.sheet_schema import (
    ADVERTISING_REPORT_LABEL_COLUMNS,
    COLUMNS,
    LAST_COLUMN,
    REPORT_METRIC_LABELS,
)


@dataclass(frozen=True, slots=True)
class SheetSnapshot:
    rows: list[list[Any]]
    merge_requests: list[dict[str, Any]]

    @property
    def last_used_row(self) -> int:
        return len(self.rows)


def build_sheet_snapshot(
    order_groups: OrderGroups,
    *,
    operational_day: date,
    sheet_id: int,
    spreadsheet_id: str,
) -> SheetSnapshot:
    groups_by_day = _groups_by_day(order_groups)
    earliest_day = min([operational_day, *order_groups.days.values()])
    rows: list[list[Any]] = []
    merge_requests: list[dict[str, Any]] = []
    report_rows: list[tuple[int, str, date]] = []
    month_rows: list[int] = []
    day_rows: list[int] = []
    current_day = earliest_day
    current_month: tuple[int, int] | None = None

    while current_day <= operational_day:
        month = (current_day.year, current_day.month)
        if month != current_month:
            month_row = [""] * LAST_COLUMN
            month_row[0] = "Місяць"
            month_row[1] = month_period_label(current_day)
            month_row[COLUMNS.row_type - 1] = ROW_MONTH
            month_row[COLUMNS.operational_date - 1] = sheet_serial(current_day.replace(day=1))
            rows.append(month_row)
            month_rows.append(len(rows))
            current_month = month

        day_row = [""] * LAST_COLUMN
        day_row[0] = "Дата дня"
        day_row[1] = sheet_serial(current_day)
        day_row[COLUMNS.row_type - 1] = ROW_DAY
        day_row[COLUMNS.operational_date - 1] = sheet_serial(current_day)
        rows.extend([day_row, list(ALL_HEADERS)])
        day_rows.append(len(rows) - 1)

        for _, group in groups_by_day.get(current_day, []):
            order_start = len(rows) + 1
            _append_order_group(rows, group)
            order_end = len(rows)
            if order_end > order_start:
                for column in (COLUMNS.order_number, COLUMNS.order_total):
                    merge_requests.append(
                        _merge_request(sheet_id, order_start, order_end, column - 1, column)
                    )

        if current_day < operational_day:
            _append_report_rows(
                rows,
                merge_requests,
                report_rows,
                report_day=current_day,
                sheet_id=sheet_id,
            )
        current_day += timedelta(days=1)

    for row_number, row_type, report_day in report_rows:
        formulas = report_formulas(report_day, first_data_row=1, last_data_row=len(rows))
        for column, formula in formulas[row_type].items():
            rows[row_number - 1][column - 1] = formula

    _add_selection_links(
        rows,
        month_rows=month_rows,
        day_rows=day_rows,
        spreadsheet_id=spreadsheet_id,
        sheet_id=sheet_id,
    )
    return SheetSnapshot(rows, merge_requests)


def _groups_by_day(order_groups: OrderGroups) -> dict[date, list[tuple[str, list[list[Any]]]]]:
    result: dict[date, list[tuple[str, list[list[Any]]]]] = {}
    for key, group in order_groups.rows.items():
        result.setdefault(order_groups.days[key], []).append((key, group))
    for day_groups in result.values():
        day_groups.sort(key=lambda pair: (order_groups.sort_values[pair[0]], pair[0]))
    return result


def _append_order_group(rows: list[list[Any]], group: list[list[Any]]) -> None:
    first_order_number = group[0][COLUMNS.order_number - 1]
    first_order_total = group[0][COLUMNS.order_total - 1]
    for item_index, source_row in enumerate(group):
        row = list(source_row)
        final_row = len(rows) + 1
        row[COLUMNS.markup - 1] = markup_formula(final_row)
        row[COLUMNS.order_number - 1] = first_order_number if item_index == 0 else ""
        row[COLUMNS.order_total - 1] = first_order_total if item_index == 0 else ""
        rows.append(row)


def _append_report_rows(
    rows: list[list[Any]],
    merge_requests: list[dict[str, Any]],
    report_rows: list[tuple[int, str, date]],
    *,
    report_day: date,
    sheet_id: int,
) -> None:
    labels = {
        ROW_REPORT_DAY: f"Підсумок за {report_day:%d.%m.%Y}",
        ROW_REPORT_MTD: f"Разом за {report_day.day} дн. місяця",
        ROW_REPORT_FORECAST: "Прогноз на місяць",
    }
    for row_type in (ROW_REPORT_DAY, ROW_REPORT_MTD, ROW_REPORT_FORECAST):
        row = [""] * LAST_COLUMN
        row[0] = labels[row_type]
        for column, label in REPORT_METRIC_LABELS.items():
            if row_type != ROW_REPORT_FORECAST or column not in ADVERTISING_REPORT_LABEL_COLUMNS:
                row[column - 1] = label
        row[COLUMNS.row_type - 1] = row_type
        row[COLUMNS.operational_date - 1] = sheet_serial(report_day)
        rows.append(row)
        row_number = len(rows)
        report_rows.append((row_number, row_type, report_day))
        merge_requests.append(_merge_request(sheet_id, row_number, row_number, 0, 2))


def _add_selection_links(
    rows: list[list[Any]],
    *,
    month_rows: list[int],
    day_rows: list[int],
    spreadsheet_id: str,
    sheet_id: int,
) -> None:
    def link(start_row: int, end_row: int, label: str) -> str:
        url = f"#gid={sheet_id}&range=A{start_row}:T{end_row}"
        return f'=HYPERLINK("{url}";"{label}")'

    def cell_link(row_number: int, label: str) -> str:
        url = f"#gid={sheet_id}&range=A{row_number}"
        return f'=HYPERLINK("{url}";"{label}")'

    for index, row_number in enumerate(month_rows):
        end = month_rows[index + 1] - 1 if index + 1 < len(month_rows) else len(rows)
        rows[row_number - 1][2] = link(row_number, end, "Виділити місяць")
    section_starts = sorted([*month_rows, *day_rows])
    for row_number in day_rows:
        next_start = next((start for start in section_starts if start > row_number), len(rows) + 1)
        rows[row_number - 1][2] = link(row_number, next_start - 1, "Виділити день")
    if rows and day_rows:
        rows[0][3] = cell_link(len(rows), "↓ До кінця")


def _merge_request(
    sheet_id: int, start_row: int, end_row: int, start_column: int, end_column: int
) -> dict[str, Any]:
    return {
        "mergeCells": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": start_row - 1,
                "endRowIndex": end_row,
                "startColumnIndex": start_column,
                "endColumnIndex": end_column,
            },
            "mergeType": "MERGE_ALL",
        }
    }
