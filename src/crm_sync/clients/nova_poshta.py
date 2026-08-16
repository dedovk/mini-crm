from __future__ import annotations

import logging
from collections.abc import Iterable
from decimal import Decimal

from crm_sync.clients.http import ApiError, HttpClient
from crm_sync.models import ShipmentStatus
from crm_sync.utils import decimal_value, extract_ttn, first_value, normalize_shipment_status

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
        failed_batches: list[str] = []
        successful_batches = 0
        for start in range(0, len(numbers), 100):
            batch = numbers[start : start + 100]
            try:
                payload = self.http.request_json(
                    "POST",
                    self.url,
                    json={
                        "apiKey": self.api_key,
                        "modelName": "TrackingDocument",
                        "calledMethod": "getStatusDocuments",
                        "methodProperties": {
                            "Documents": [{"DocumentNumber": number} for number in batch]
                        },
                    },
                )
                if not isinstance(payload, dict) or not payload.get("success"):
                    errors = payload.get("errors") if isinstance(payload, dict) else payload
                    raise ApiError(f"Nova Poshta tracking failed: {errors}")
                data = payload.get("data")
                if not isinstance(data, list):
                    raise ApiError("Nova Poshta tracking data must be a list")
            except ApiError as exc:
                batch_number = start // 100 + 1
                failed_batches.append(str(batch_number))
                LOGGER.warning("Nova Poshta tracking batch %s failed: %s", batch_number, exc)
                continue
            successful_batches += 1
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
                    redelivery_sum=decimal_value(
                        first_value(
                            item,
                            "RedeliverySum",
                            "RedeliveryAmount",
                            "AfterpaymentOnGoodsCost",
                            default=Decimal(0),
                        )
                    ),
                )
        if failed_batches and not successful_batches:
            raise ApiError(
                "Nova Poshta tracking failed for all batches: " + ", ".join(failed_batches)
            )
        if failed_batches:
            LOGGER.warning(
                "Nova Poshta returned partial tracking data; failed batch(es): %s",
                ", ".join(failed_batches),
            )
        LOGGER.info(
            "Nova Poshta tracking data contains COD amount for %s/%s shipment(s)",
            sum(status.redelivery_sum > 0 for status in result.values()),
            len(result),
        )
        return result
