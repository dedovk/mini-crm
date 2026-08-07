# Marketplace CRM Sync MVP

Python 3 service that synchronizes completed orders with created TTNs from Prom.ua,
Rozetka Seller and ocStore 2.1 into an existing Google Sheet. It runs every 15 minutes
through GitHub Actions.

## Implemented behavior

- One product is written per row. A multi-product order occupies consecutive rows.
- The marketplace order number (column E) and order total (column N) are merged across
  the product rows of one order.
- Only marketplace orders in the completed/successful status are imported.
- Column D contains the completion date when the source exposes an exact status-change
  timestamp; unknown completion dates are left blank instead of being invented.
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
- Prom.ua advertising expense is written to column S from `prosale_commission.value`
  (with `cpa_commission.amount` as a compatibility fallback). Report rows split it into
  ProSale and exact fixed `10.00` Prom charges; Rozetka expenses are reported separately.
  Advertising values are shown in daily and month-to-date rows without a monthly forecast.
- Existing order rows are refreshed from their source for tracking number, combined
  `city, surname first name`, product code, payment method and the markup formula without
  overwriting manual costs.
- Dropdown validation is configured for Nova Poshta status, sender and payment method.
- Individual source failures are logged and do not stop the other marketplaces.
- Orders are grouped by the date when they became completed. At local midnight the previous
  day receives formula-driven daily, month-to-date and month forecast rows. Summary rows
  are consecutive and place all KPI labels and values together in columns A:P.
- Daily sections advance even when no new orders arrive; the rollover closes prior days and
  creates the current day section during the first production run after local midnight.
- Multi-item orders receive a black outer border across the complete order block.
- Month and day title rows contain styled `Виділити місяць` / `Виділити день` links that
  select the corresponding A:T range.
- The sheet uses compact Ukrainian headers, 8-point body text, fixed narrow widths,
  status colors and hidden technical columns U:W so all business columns fit horizontally.
- Sheet rebuilds write the new snapshot before clearing only obsolete trailing rows, so a
  failed write cannot erase the existing worksheet contents.
- Every GitHub Actions run publishes a structured step summary with source counts, skipped
  stale orders, shipment updates, appended rows and non-fatal integration warnings.
- A separate `Журнал змін` worksheet records newly appended orders and Nova Poshta status
  transitions with timestamps, sync keys, TTNs and old/new values. Audit failures are
  non-fatal and are surfaced in the GitHub Actions summary.
- Every run performs a read-only integrity preflight before any mutation and a second
  validation after production writes. Formula errors, negative unit costs, split duplicate
  order blocks and conflicting totals stop the sync before more data is written.
- Hidden columns X:Y persist the first observed completion date and the last marketplace
  order status. For sources without status history, the first real observation of `Виконано`
  is used instead of inventing a date from order creation.
- No structural worksheet rebuild is performed when there are no new orders. A hidden
  timestamped backup is created before an actual rebuild, and the three newest copies are kept.
- Production integration failures are aggregated. One GitHub Issue is opened on the third
  consecutive degraded run and automatically closed after recovery, avoiding an email for
  every 15-minute attempt.

## Google Sheet contract

The target spreadsheet is selected only through the `GOOGLE_SPREADSHEET_ID` GitHub Secret.
Worksheet `БСК` contains the CRM data, with the first headers near the top. Columns A:T are business fields.
Columns U:Y are hidden technical fields for duplicate protection, row type, the
operational date used by daily reports, first completion observation and source status.

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
SENDER_DEFAULT=наш
SENDER_OPTIONS=imaxi-com,Melad,Melad дроп,наш
```

Strongly recommended for Rozetka JWT renewal:

```text
ROZETKA_USERNAME
ROZETKA_PASSWORD
```

`ROZETKA_API_TOKEN` can be used directly, but a JWT can expire. When username/password
are configured, the client obtains a new token through `POST /sites`; Rozetka requires
the password field to be Base64-encoded in this request.

Non-secret runtime defaults are documented in the workflow and in `Settings.from_env()`.

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

DirectAdmin is the primary scheduler and dispatches the GitHub workflow every 15 minutes
through `workflow_dispatch`. Frequent runs read the latest seven days. GitHub keeps only
one timezone-aware daily run at 02:13 `Europe/Kyiv` to reconcile the full 30-day history,
including TTNs added to older orders. The workflow also supports manual dispatch with a
`dry_run` checkbox. A concurrency group prevents overlapping writes.

## Prom.ua

The client uses `GET /orders/list?status=delivered` with `Authorization: Bearer ...`, handles
pagination, normalizes product rows and retains only completed orders containing a supported TTN. Prom prices
such as `1 149 грн` are converted to numeric values, the product code comes from `sku`,
and the ProSale commission is recorded as advertising expense. Frequent automation scans
seven days to stay within API limits, while the daily deep reconciliation scans 30 days.
The HTTP client honors Prom's `Retry-After` response and uses a longer fallback pause for
rate limits. Duplicate filtering makes both overlaps safe. Prom's public Order model does
not expose order-status history, so a newly completed Prom order uses the first polling time
at which the script sees `delivered`; later runs keep that recorded time stable.

## Rozetka

The client uses `GET /orders/search` with `types=3` and
`expand=user,delivery,purchases,status_data`, handles pagination and retains only successfully
completed orders with a TTN. The API `changed` value supplies the completion time, while an
available `order_status_history` timestamp takes precedence. It accepts an existing token and
supports the current `POST /sites` login flow when login/password secrets are configured.
The current official base URL is `https://api-seller.rozetka.com.ua`; the former
`api.seller.rozetka.com.ua` hostname serves an expired certificate and is not used.

Rozetka finance synchronization reads sale commissions from `/balances/search` and
shipment delivery charges from `/balance-logistic/search`. Transactions are deduplicated
by their API identifiers and grouped by Rozetka order ID. A credited logistics adjustment
reduces the original delivery expense without removing the order from the worksheet. The
resulting non-negative total is written once, on the first item row, in the advertising
expense column. Frequent runs reconcile 45 days and the daily run reconciles 90 days;
configure this with `ROZETKA_FINANCE_LOOKBACK_DAYS` when a longer backfill is required.
Finance access is optional: if the Rozetka account does not expose logistics history,
the order sync succeeds and preserves the existing advertising-expense cells.

Daily deep reconciliation reads older orders to refresh TTNs and statuses, but does not
insert previously unseen stale orders. `NEW_ORDER_MAX_AGE_DAYS` controls this independent
new-order window and defaults to seven days.

## ocStore 2.1.0.2.1

The stock API token does not expose historical order listing. Install the read-only
controller from [opencart_extension](opencart_extension/README.md) on
`https://ibox-shop.co.ua/`. The endpoint validates the active ocStore API key from the
`api` table and only reads orders/products/comments.

The read-only endpoint returns only completed orders and derives `completed_at` from the
latest history entry for the current completed status. The installed Nova Poshta module
stores TTNs in the `novaposhta_cn_number` order field; the normalizer reads this field directly.
Rejected completion statuses are logged as aggregate status names without customer data,
which makes a zero-result OpenCart run diagnosable from GitHub Actions.

## Production rollout

1. Add `ROZETKA_USERNAME` and `ROZETKA_PASSWORD` if the current token is a JWT.
2. Install and test the ocStore endpoint.
3. Run GitHub Actions manually with `dry_run=true` and inspect logs.
   Set `lookback_days=30` when diagnosing older OpenCart or marketplace orders; the
   independent seven-day new-order cutoff prevents accidental stale backfill.
4. Run once with `dry_run=false` on a small known set of orders.
5. Leave the scheduled workflow enabled after confirming row formatting and merging.
