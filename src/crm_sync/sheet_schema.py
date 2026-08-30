from __future__ import annotations

from dataclasses import dataclass


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
    product_code: int = 9
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
    net_profit: int = 20
    manager_note: int = 21
    sync_key: int = 22
    row_type: int = 23
    operational_date: int = 24
    first_seen_completed: int = 25
    order_status: int = 26
    advertising_base: int = 27
    receipt: int = 28
    installment_commission: int = 29
    installment_commission_source: int = 30
    reporting_state: int = 31
    supplier_cost_source: int = 32
    supplier_cost_currency: int = 33
    supplier_cost_original: int = 34


COLUMNS = SheetColumns()
LAST_COLUMN = COLUMNS.supplier_cost_original
LAST_COLUMN_LETTER = "AH"

PAYMENT_OPTIONS = (
    "пром оплата(оплата картой)",
    "оплата частями",
    "наложка",
    "оплата на счет",
    "смешанная",
    "Зачет",
)

NOVA_POSHTA_STATUS_OPTIONS = (
    "Створено електронну накладну",
    "Нова Пошта очікує надходження",
    "Прямує до покупця",
    "Отримано",
    "Відмова від отримання",
    "Повертається відправнику",
    "Повернуто відправнику",
    "Невідомо",
    "Інший перевізник",
)

REPORT_METRIC_LABELS = {
    3: "Замовлень",
    5: "Сума, грн",
    7: "Собівартість, грн",
    9: "Націнка, грн",
    11: "ProSale, грн",
    13: "Rozetka, грн",
    15: "Prom 10 грн",
    17: "Оплата част., грн",
    19: "Чистий прибуток, грн",
}
ADVERTISING_REPORT_LABEL_COLUMNS = {11, 13, 15, 17}
FORECAST_EXCLUDED_REPORT_LABEL_COLUMNS = {*ADVERTISING_REPORT_LABEL_COLUMNS, 19}
FORECAST_EXCLUDED_REPORT_VALUE_COLUMNS = {
    column + 1 for column in FORECAST_EXCLUDED_REPORT_LABEL_COLUMNS
}

AUDIT_WORKSHEET_NAME = "Журнал змін"
AUDIT_HEADERS = (
    "Час",
    "Подія",
    "Джерело",
    "№ замовлення",
    "Sync Key",
    "ТТН",
    "Поле",
    "Старе значення",
    "Нове значення",
    "Деталі",
)

BACKUP_PREFIX = "_CRM backup - "
BACKUP_RETENTION = 3
HEALTH_WORKSHEET_NAME = "_Стан синхронізації"
HEALTH_ALERT_THRESHOLD = 3
