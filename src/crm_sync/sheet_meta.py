from __future__ import annotations

from datetime import datetime
from typing import Any

from gspread.exceptions import WorksheetNotFound

from crm_sync.models import OrderAuditEvent, SyncHealthState
from crm_sync.sheet_layout import source_display
from crm_sync.sheet_schema import (
    AUDIT_HEADERS,
    AUDIT_WORKSHEET_NAME,
    BACKUP_PREFIX,
    BACKUP_RETENTION,
    HEALTH_ALERT_THRESHOLD,
    HEALTH_WORKSHEET_NAME,
)

HEADER_FORMAT = {
    "backgroundColor": {"red": 0.12, "green": 0.31, "blue": 0.47},
    "textFormat": {
        "bold": True,
        "foregroundColor": {"red": 1, "green": 1, "blue": 1},
    },
}


def append_sheet_audit_events(spreadsheet: Any, events: list[OrderAuditEvent]) -> int:
    if not events:
        return 0
    audit = ensure_sheet_audit_worksheet(spreadsheet)
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


def ensure_sheet_audit_worksheet(spreadsheet: Any) -> Any:
    created = False
    try:
        audit = spreadsheet.worksheet(AUDIT_WORKSHEET_NAME)
    except WorksheetNotFound:
        created = True
        audit = spreadsheet.add_worksheet(
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
                **HEADER_FORMAT,
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
            },
        )
    return audit


def create_sheet_backup(spreadsheet: Any, worksheet: Any, *, created_at: datetime) -> str:
    source_title = str(getattr(worksheet, "title", "CRM"))
    title = f"{BACKUP_PREFIX}{created_at:%Y%m%d-%H%M%S} - {source_title}"[:100]
    backup = spreadsheet.duplicate_sheet(
        source_sheet_id=worksheet.id,
        new_sheet_name=title,
    )
    spreadsheet.batch_update(
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
            candidate
            for candidate in spreadsheet.worksheets()
            if str(getattr(candidate, "title", "")).startswith(BACKUP_PREFIX)
        ),
        key=lambda candidate: str(getattr(candidate, "title", "")),
        reverse=True,
    )
    for obsolete in backups[BACKUP_RETENTION:]:
        spreadsheet.del_worksheet(obsolete)
    return title


def record_sheet_sync_health(
    spreadsheet: Any,
    failed_components: list[str],
    *,
    occurred_at: datetime,
) -> SyncHealthState:
    health, created = _health_worksheet(spreadsheet)
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

    health.update(
        values=[
            ["Параметр", "Значення"],
            ["consecutive_failures", str(consecutive)],
            ["alert_open", str(alert_open).lower()],
            ["last_outcome", outcome],
            ["failed_components", ", ".join(components)],
            ["updated_at", occurred_at.isoformat()],
        ],
        range_name="A1:B6",
        raw=True,
    )
    if created:
        health.freeze(rows=1)
        health.format("A1:B1", HEADER_FORMAT)
        spreadsheet.batch_update(
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


def _health_worksheet(spreadsheet: Any) -> tuple[Any, bool]:
    try:
        return spreadsheet.worksheet(HEALTH_WORKSHEET_NAME), False
    except WorksheetNotFound:
        return (
            spreadsheet.add_worksheet(title=HEALTH_WORKSHEET_NAME, rows=10, cols=2),
            True,
        )
