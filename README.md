# Mini-CRM для замовлень

Mini-CRM автоматично збирає замовлення з Prom.ua, Rozetka та магазину на ocStore,
оновлює статуси доставки Нової пошти й веде спільний облік у Google Sheets. Проєкт
прибирає ручне копіювання замовлень, захищає таблицю від дублікатів і допомагає бачити
продажі, комісії та підсумки в одному місці.

## Функціональність

- імпорт замовлень із Prom.ua, Rozetka Seller API та ocStore 2.1;
- одне замовлення з кількома товарами записується окремим блоком рядків;
- захист від дублікатів за ключем `джерело + ID замовлення`;
- нормалізація телефонів, дат, сум, способів оплати, передоплати й номерів ТТН;
- оновлення статусів Нової пошти для наявних замовлень;
- видалення з розрахунків замовлень із відмовою від отримання;
- облік ProSale, комісій Rozetka, логістики та оплати частинами; фінанси Rozetka
  звіряються у вікні 45 днів для частих запусків і 90 днів для щоденного запуску,
  а не зберігаються як повна історія всіх транзакцій;
- збереження ручної собівартості й приміток менеджера;
- заповнення порожньої собівартості за ТТН із таблиці постачальника IMAXI;
- автоматичні денні та місячні підсумки в Google Sheets;
- журнал змін, резервні копії перед структурною перебудовою вкладки та перевірка
  цілісності даних;
- збій окремого джерела замовлень або Нової пошти не блокує інші джерела;
  Google Sheets залишається обов’язковим сховищем для запуску;
- `dry-run` для безпечної перевірки без запису в таблицю;
- GitHub Issue після трьох послідовних production-запусків із деградацією джерел, фінансів або
  Нової пошти; критична помилка конфігурації чи Google Sheets позначає Actions run як failed.

## Стек

- Python 3.11+;
- `requests` для marketplace та Nova Poshta API;
- `gspread` і `google-auth` для Google Sheets;
- `pytest` для тестів, Ruff для перевірки коду;
- GitHub Actions для запусків і CI;
- PHP-контролер для read-only доступу до замовлень ocStore.

Код розділений на API-клієнти, сервіс синхронізації, моделі, перевірки даних і модулі
побудови Google Sheets. Розширення для ocStore описане в
[opencart_extension/README.md](opencart_extension/README.md).

## Налаштування

Секрети зберігаються тільки у GitHub Secrets або локальних змінних оточення:

```text
GOOGLE_SERVICE_ACCOUNT_JSON_B64
GOOGLE_SPREADSHEET_ID
GOOGLE_WORKSHEET_NAME
SUPPLIER_IMAXI_SPREADSHEET_ID
PROM_API_TOKEN
ROZETKA_API_TOKEN
ROZETKA_USERNAME
ROZETKA_PASSWORD
OPENCART_BASE_URL
OPENCART_API_KEY
NP_API_TOKEN
APP_TIMEZONE
```

Service Account повинен мати права редактора потрібної Google-таблиці. Часті запуски
надсилає DirectAdmin, а GitHub Actions виконує синхронізацію, ручний `dry-run` і щоденну
поглиблену перевірку старіших замовлень. Concurrency-група не дозволяє двом запускам
одночасно записувати дані в таблицю.

## Локальний запуск

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
$env:PYTHONPATH = "src"
$env:DRY_RUN = "true"
python -m crm_sync.main
```

Перевірка проєкту:

```powershell
python -m pytest -q
python -m ruff check src tests
```

Не додавайте `.env`, JSON-ключ Service Account, токени або реальні ідентифікатори таблиць
до Git. Репозиторій містить лише назви потрібних змінних оточення.
