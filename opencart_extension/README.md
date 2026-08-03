# ocStore 2.1.0.2.1 read-only CRM endpoint

The stock ocStore/OpenCart 2.1 API token does not expose a list of historical orders.
This small controller adds a read-only endpoint protected by the active API key already
created in **System -> Users -> API**.

## Install

1. Back up the store files and database.
2. Upload the contents of `upload/` into the ocStore root, preserving directories.
3. Do not overwrite any existing `crm_orders.php` without reviewing it first.
4. Test:

```bash
curl -H "X-CRM-API-Key: YOUR_OPENCART_API_KEY" \
  "https://ibox-shop.co.ua/index.php?route=api/crm_orders&changed_from=2026-08-01%2000:00:00"
```

The endpoint returns only completed orders together with products and comments. It exposes
the latest history timestamp for the current completed status as `completed_at` and never
writes to the store database. On the current store, the Nova Poshta module stores the TTN in
the `novaposhta_cn_number` order field; the Python client reads it directly.
