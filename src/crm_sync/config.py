from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any


class ConfigurationError(RuntimeError):
    """Raised when required runtime configuration is invalid."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.casefold() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    google_service_account_info: dict[str, Any]
    google_spreadsheet_id: str
    google_worksheet_name: str
    google_header_row: int
    prom_token: str
    prom_base_url: str
    rozetka_token: str
    rozetka_username: str
    rozetka_password: str
    rozetka_base_url: str
    opencart_base_url: str
    opencart_api_key: str
    opencart_orders_endpoint: str
    nova_poshta_key: str
    nova_poshta_url: str
    timezone: str
    sync_lookback_days: int
    http_timeout: int
    http_max_retries: int
    log_level: str
    sender_default: str
    sender_options: tuple[str, ...]
    dry_run: bool

    @classmethod
    def from_env(cls) -> Settings:
        credentials_b64 = _env("GOOGLE_SERVICE_ACCOUNT_JSON_B64")
        if not credentials_b64:
            raise ConfigurationError("GOOGLE_SERVICE_ACCOUNT_JSON_B64 is required")
        try:
            credentials = json.loads(base64.b64decode(credentials_b64).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError("GOOGLE_SERVICE_ACCOUNT_JSON_B64 is not valid Base64 JSON") from exc
        if credentials.get("type") != "service_account" or not credentials.get("private_key"):
            raise ConfigurationError("Google credentials are not a Service Account JSON key")

        spreadsheet_id = _env("GOOGLE_SPREADSHEET_ID")
        worksheet_name = _env("GOOGLE_WORKSHEET_NAME")
        if not spreadsheet_id or not worksheet_name:
            raise ConfigurationError("GOOGLE_SPREADSHEET_ID and GOOGLE_WORKSHEET_NAME are required")

        sender_default = _env("SENDER_DEFAULT", "наш")
        sender_options = tuple(
            dict.fromkeys(
                value.strip()
                for value in _env("SENDER_OPTIONS", "imaxi-com,Melad,Melad дроп,наш").split(",")
                if value.strip()
            )
        )
        sender_options = tuple(dict.fromkeys((*sender_options, sender_default)))

        return cls(
            google_service_account_info=credentials,
            google_spreadsheet_id=spreadsheet_id,
            google_worksheet_name=worksheet_name,
            google_header_row=_env_int("GOOGLE_HEADER_ROW", 4),
            prom_token=_env("PROM_API_TOKEN"),
            prom_base_url=_env("PROM_API_BASE_URL", "https://my.prom.ua/api/v1"),
            rozetka_token=_env("ROZETKA_API_TOKEN"),
            rozetka_username=_env("ROZETKA_USERNAME"),
            rozetka_password=_env("ROZETKA_PASSWORD"),
            rozetka_base_url=_env("ROZETKA_API_BASE_URL", "https://api-seller.rozetka.com.ua"),
            opencart_base_url=_env("OPENCART_BASE_URL"),
            opencart_api_key=_env("OPENCART_API_KEY"),
            opencart_orders_endpoint=_env("OPENCART_ORDERS_ENDPOINT", "/index.php?route=api/crm_orders"),
            nova_poshta_key=_env("NP_API_TOKEN") or _env("NOVA_POSHTA_API_KEY"),
            nova_poshta_url=_env("NOVA_POSHTA_API_URL", "https://api.novaposhta.ua/v2.0/json/"),
            timezone=_env("APP_TIMEZONE", "Europe/Kyiv"),
            sync_lookback_days=_env_int("SYNC_LOOKBACK_DAYS", 30),
            http_timeout=_env_int("HTTP_TIMEOUT_SECONDS", 30),
            http_max_retries=_env_int("HTTP_MAX_RETRIES", 4),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            sender_default=sender_default,
            sender_options=sender_options or (sender_default,),
            dry_run=_env_bool("DRY_RUN"),
        )
