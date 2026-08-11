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
    manager_note: int = 20
    sync_key: int = 21
    row_type: int = 22
    operational_date: int = 23
    first_seen_completed: int = 24
    order_status: int = 25
    advertising_base: int = 26
    installment_commission: int = 27


COLUMNS = SheetColumns()
LAST_COLUMN = COLUMNS.installment_commission
LAST_COLUMN_LETTER = "AA"

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
}
ADVERTISING_REPORT_LABEL_COLUMNS = {11, 13, 15}

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
