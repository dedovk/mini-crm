from __future__ import annotations

import logging
from collections.abc import Iterable

from crm_sync.clients.http import ApiError, HttpClient
from crm_sync.models import ShipmentStatus
from crm_sync.utils import extract_ttn, first_value, normalize_shipment_status

LOGGER = logging.getLogger(__name__)


class NovaPoshtaClient:
    def __init__(self, http: HttpClient, *, api_key: str, url: str) -> None:
        self.http = http
        self.api_key = api_key
        self.url = url

    def get_statuses(self, tracking_numbers: Iterable[str]) -> dict[str, ShipmentStatus]:
        numbers = list(
            dict.fromkeys(
                normalized
                for value in tracking_numbers
                if (normalized := extract_ttn(value))
            )
        )
        if not numbers or not self.api_key:
            if numbers and not self.api_key:
                LOGGER.warning("Nova Poshta status update skipped: NP_API_TOKEN is not configured")
            return {}
        result: dict[str, ShipmentStatus] = {}
        for start in range(0, len(numbers), 100):
            batch = numbers[start : start + 100]
            payload = self.http.request_json(
                "POST",
                self.url,
                json={
                    "apiKey": self.api_key,
                    "modelName": "TrackingDocument",
                    "calledMethod": "getStatusDocuments",
                    "methodProperties": {"Documents": [{"DocumentNumber": number} for number in batch]},
                },
            )
            if not isinstance(payload, dict) or not payload.get("success"):
                raise ApiError(f"Nova Poshta tracking failed: {payload.get('errors') if isinstance(payload, dict) else payload}")
            data = payload.get("data") or []
            for item in data:
                if not isinstance(item, dict):
                    continue
                number = str(first_value(item, "Number", "DocumentNumber", "IntDocNumber"))
                if not number:
                    continue
                status_code = str(first_value(item, "StatusCode", "StateId"))
                status = normalize_shipment_status(
                    first_value(item, "Status", "StatusDescription"), status_code
                )
                result[number] = ShipmentStatus(
                    tracking_number=number,
                    status=status,
                    status_code=status_code,
                )
        return result
