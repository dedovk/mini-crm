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
- Tracking numbers preserve the marketplace value and support Nova Poshta numbers with
  spaces, Rozetka Delivery `RMP-...`, Ukrposhta and hyphenated Meest identifiers. Only
  14-digit Nova Poshta numbers are sent to the Nova Poshta tracking API.
- Column I contains the marketplace product code. For Prom.ua this is the exact
  `products[].sku` value shown as `Артикул` in the seller cabinet.
- Monetary fields are numeric and do not contain `грн`.
- Duplicate key is `source:order_id`; it is stored in hidden column U (`Sync Key`).
- Nova Poshta statuses are refreshed for every non-final TTN already present in the sheet.
- `Отримано` and `Відмова від отримання` are treated as final.
- A note such as `перед - 500` is parsed into numeric prepayment `500`.
- Existing manual cost, advertising and manager-note cells are never overwritten.
- Markup in column R is created as `(unit price - cost) * quantity`.
- Prom.ua ProSale commission is written to advertising expenses in column S from
  `prosale_commission.value` (with `cpa_commission.amount` as a compatibility fallback).
- Existing order rows are refreshed from their source for tracking number, combined
  `city, surname first name`, product code, payment method and the markup formula without
  overwriting manual costs.
- Dropdown validation is configured for Nova Poshta status, sender and payment method.
- Individual source failures are logged and do not stop the other marketplaces.
- Orders are grouped by their actual marketplace order date. At local midnight the previous
  day receives formula-driven daily, month-to-date and month forecast rows. Summary rows
  have one blank row between them, and day sections have four blank rows between them.
- Multi-item orders receive a black outer border across the complete order block.
- The sheet uses concise Ukrainian headers, centered wrapped content, fixed readable
  column widths, status colors and hidden technical columns U:W.

## Google Sheet contract

The current template is spreadsheet `<GOOGLE_SPREADSHEET_ID_REMOVED>`,
worksheet `БСК`, with the first headers on row 4. Columns A:T are business fields.
Columns U:W are hidden technical fields for duplicate protection, row type and the
operational date used by daily reports.

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
SENDER_DEFAULT=-
SENDER_OPTIONS=-,imaxi-com,another-sender
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

The workflow is in `.github/workflows/cron.yml` and requests runs at minutes 11, 26, 41
and 56. The offset avoids GitHub's busiest start-of-hour window. Frequent runs read the
latest seven days; a timezone-aware daily run at 02:13 `Europe/Kyiv` reconciles the full
30-day history, including TTNs added to older orders. GitHub may still delay scheduled
events, so the synchronization itself is idempotent and safe after a delayed run.
The workflow also supports manual `workflow_dispatch` with a `dry_run` checkbox. A
concurrency group prevents overlapping writes.

## Prom.ua

The client uses `GET /orders/list` with `Authorization: Bearer ...`, handles pagination,
normalizes product rows and retains only orders containing a supported TTN. Prom prices
such as `1 149 грн` are converted to numeric values, the product code comes from `sku`,
and the ProSale commission is recorded as advertising expense. Frequent automation scans
seven days to stay within API limits, while the daily deep reconciliation scans 30 days.
The HTTP client honors Prom's `Retry-After` response and uses a longer fallback pause for
rate limits. Duplicate filtering makes both overlaps safe.

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
