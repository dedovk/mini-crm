from __future__ import annotations

import logging
import sys

from crm_sync.clients.google_sheets import GoogleSheetsGateway
from crm_sync.clients.http import HttpClient
from crm_sync.clients.nova_poshta import NovaPoshtaClient
from crm_sync.clients.opencart import OpenCartClient
from crm_sync.clients.prom import PromClient
from crm_sync.clients.rozetka import RozetkaClient
from crm_sync.config import ConfigurationError, Settings
from crm_sync.reporting import write_github_summary
from crm_sync.services import SourceSyncError, SyncService

LOGGER = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def build_service(settings: Settings) -> SyncService:
    http = HttpClient(timeout=settings.http_timeout, max_retries=settings.http_max_retries)
    sheets = GoogleSheetsGateway(
        credentials_info=settings.google_service_account_info,
        spreadsheet_id=settings.google_spreadsheet_id,
        worksheet_name=settings.google_worksheet_name,
        header_row=settings.google_header_row,
        sender_options=settings.sender_options,
    )
    rozetka = RozetkaClient(
        http,
        token=settings.rozetka_token,
        username=settings.rozetka_username,
        password=settings.rozetka_password,
        base_url=settings.rozetka_base_url,
        timezone=settings.timezone,
    )
    sources = [
        PromClient(
            http,
            token=settings.prom_token,
            base_url=settings.prom_base_url,
            timezone=settings.timezone,
        ),
        rozetka,
        OpenCartClient(
            http,
            base_url=settings.opencart_base_url,
            api_key=settings.opencart_api_key,
            endpoint=settings.opencart_orders_endpoint,
            timezone=settings.timezone,
        ),
    ]
    return SyncService(
        sheets=sheets,
        nova_poshta=NovaPoshtaClient(
            http,
            api_key=settings.nova_poshta_key,
            url=settings.nova_poshta_url,
        ),
        sources=sources,
        expense_source=rozetka,
        timezone=settings.timezone,
        lookback_days=settings.sync_lookback_days,
        new_order_max_age_days=settings.new_order_max_age_days,
        expense_lookback_days=settings.rozetka_finance_lookback_days,
        sender_default=settings.sender_default,
        dry_run=settings.dry_run,
    )


def main() -> int:
    try:
        settings = Settings.from_env()
        configure_logging(settings.log_level)
        result = build_service(settings).run()
        write_github_summary(result)
        return 0
    except SourceSyncError as exc:
        write_github_summary(exc.result)
        LOGGER.error("CRM synchronization completed with source failures: %s", exc)
        return 1
    except ConfigurationError as exc:
        logging.basicConfig(level=logging.ERROR)
        LOGGER.error("Configuration error: %s", exc)
        return 2
    except Exception:
        LOGGER.exception("CRM synchronization failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
