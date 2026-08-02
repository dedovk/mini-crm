# Marketplace CRM Sync MVP

Python 3 service that synchronizes orders with created Nova Poshta TTNs from Prom.ua,
Rozetka Seller and ocStore 2.1 into an existing Google Sheet. It runs every 15 minutes
through GitHub Actions.

## Implemented behavior

- One product is written per row. A multi-product order occupies consecutive rows.
- The marketplace order number (column E) and order total (column N) are merged across
  the product rows of one order.
- Dates are converted to `Europe/Kyiv` and written as `DD.MM.YYYY HH:MM`.
- Phones are written as text in `+380...` format.
- Monetary fields are numeric and do not contain `грн`.
- Duplicate key is `source:order_id`; it is stored in hidden column U (`Sync Key`).
- Nova Poshta statuses are refreshed for every non-final TTN already present in the sheet.
- `Отримано` and `Відмова від отримання` are treated as final.
- A note such as `перед - 500` is parsed into numeric prepayment `500`.
- Existing manual cost, advertising and manager-note cells are never overwritten.
- Markup in column R is created as `(unit price - cost) * quantity`.
- Dropdown validation is configured for Nova Poshta status, sender and payment method.
- Individual source failures are logged and do not stop the other marketplaces.

## Google Sheet contract

The current template is spreadsheet `<GOOGLE_SPREADSHEET_ID_REMOVED>`,
worksheet `БСК`, with headers on row 4. Columns A:T remain the business template;
column U is added as a hidden technical key.

The service account email must have **Editor** access to the spreadsheet.

## Required GitHub Secrets

Already used by the workflow:

```text
GOOGLE_SERVICE_ACCOUNT_JSON_B64
GOOGLE_SPREADSHEET_ID
GOOGLE_WORKSHEET_NAME
APP_TIMEZONE
PROM_API_TOKEN
ROZETKA_API_TOKEN
NP_API_TOKEN
OPENCART_BASE_URL
OPENCART_API_KEY
```

Optional sender dropdown configuration:

```text
SENDER_DEFAULT=imaxi-com
SENDER_OPTIONS=imaxi-com,another-sender
```

Strongly recommended for Rozetka JWT renewal:

```text
ROZETKA_USERNAME
ROZETKA_PASSWORD
```

`ROZETKA_API_TOKEN` can be used directly, but a JWT can expire. When username/password
are configured, the client can obtain a new token through `/site/login`.

Non-secret defaults are documented in [.env.example](.env.example).

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

Populate environment variables without committing them. Run tests:

```powershell
python -m pytest -q
```

Run a read-only integration check:

```powershell
$env:DRY_RUN = "true"
$env:PYTHONPATH = "src"
python -m crm_sync.main
```

Dry-run does not add the technical column, validations, rows or status updates.

## GitHub Actions

The workflow is in `.github/workflows/cron.yml` and runs at minutes 7, 22, 37 and 52.
This is every 15 minutes while avoiding the start of the hour. It also supports manual
`workflow_dispatch` with a `dry_run` checkbox. A concurrency group prevents overlapping
writes.

## Prom.ua

The client uses `GET /orders/list` with `Authorization: Bearer ...`, handles pagination,
normalizes product rows and retains only orders containing a 14-digit TTN. It scans a
30-day lookback window by default; duplicate filtering makes the overlap safe.

## Rozetka

The client uses `GET /orders/search` with `expand=user,delivery,purchases,status_data`,
handles pagination and retains only orders with a TTN. It accepts an existing token and
supports `/site/login` fallback when login/password secrets are configured.
The current official base URL is `https://api-seller.rozetka.com.ua`; the former
`api.seller.rozetka.com.ua` hostname serves an expired certificate and is not used.

## ocStore 2.1.0.2.1

The stock API token does not expose historical order listing. Install the read-only
controller from [opencart_extension](opencart_extension/README.md) on
`https://ibox-shop.co.ua/`. The endpoint validates the active ocStore API key from the
`api` table and only reads orders/products/comments.

The installed Nova Poshta module stores TTNs in the `novaposhta_cn_number` order field.
The OpenCart normalizer reads this field directly and retains comment/history extraction
as a fallback.

## Production rollout

1. Add `ROZETKA_USERNAME` and `ROZETKA_PASSWORD` if the current token is a JWT.
2. Install and test the ocStore endpoint.
3. Run GitHub Actions manually with `dry_run=true` and inspect logs.
4. Run once with `dry_run=false` on a small known set of orders.
5. Leave the scheduled workflow enabled after confirming row formatting and merging.
