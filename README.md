# Google Play IAP Pricing Updater

A GitHub Action that automatically syncs Google Play one-time in-app product prices across products with a configurable discount — applied individually for every region using each country's actual local price.

## Why this exists

If you have two in-app products where one should always be priced at a discount relative to the other, keeping that relationship consistent across every country by hand is tedious and error-prone. Exchange rates drift, you tweak the base price, and the discount silently varies from region to region.

This action reads the source product's **actual local price in every region**, computes the discounted amount, and sets it explicitly on the target product for each country. The result is a consistent, predictable discount everywhere your app is sold.

Unlike Apple, Google Play has **no fixed price tiers** — any amount is allowed — so the computed price is simply rounded to each currency's minor unit (cents for USD, whole yen for JPY, and so on). That makes this much faster than the App Store equivalent: a handful of API calls per rule instead of per-territory tier lookups.

## How it works

For each rule you provide in the file, the action computes the target price as:

```
price(target, region) = price(source, region) × factor
```

where `factor` is either a discount (`1 − discountPercent / 100`) or an explicit `multiplier` — for example `multiplier: 3` makes the target always **3× the source**. Each rule sets exactly one of the two.

The script reads the source product's current price in **every region** it is sold in, applies the factor, rounds to the region's currency unit (with an optional `up`/`down` strategy), and writes the new prices onto the target product's purchase option in a single `PATCH`. Availability and every other field on the target are preserved.

This targets the **`monetization.onetimeproducts`** API and handles **one-time in-app products only**. Subscriptions use a different API and are not covered.

## Setup

### 1. Create a service account

1. In the [Google Cloud Console](https://console.cloud.google.com/), select (or create) the project linked to your Play developer account and enable the **Google Play Android Developer API**.
2. **IAM & Admin → Service Accounts → Create service account.**
3. Open the service account → **Keys → Add key → Create new key → JSON** and download the file.

### 2. Grant it Play Console access

1. **Play Console → Setup → API access**, link the Google Cloud project if prompted, and find the service account in the list.
2. Grant access and assign a role/permission that can **view financial data and manage products** for the app whose products you are pricing.

### 3. Add the GitHub Secret

| Secret | Value |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The full contents of the downloaded service-account JSON key file |

### 4. Create your pricing rules file

Add `play-pricing-rules.json` to the root of your repository:

```json
[
  {
    "packageName": "com.example.app",
    "sourceProductId": "premium_unlock",
    "targetProductId": "premium_unlock_sale",
    "discountPercent": 10
  }
]
```

Multiple rules are supported — each runs independently and failures don't block the rest.

#### Finding a product ID

**Play Console → your app → Monetize → Products → In-app products** — the `Product ID` column shows the string ID (e.g. `premium_unlock`). `packageName` is your app's application ID (e.g. `com.example.app`).

### 5. Add the workflow

```yaml
name: Update Google Play IAP Pricing

on:
  schedule:
    - cron: '0 10 * * 1'   # every Monday at 10:00 UTC
  workflow_dispatch:
    inputs:
      dry_run:
        description: 'Dry run (log actions without changing anything)'
        type: boolean
        default: false

permissions:
  contents: read

jobs:
  update-pricing:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6

      - uses: Idlevelopment/play-discount-sync@v1
        with:
          service-account-json: ${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}
          dry-run: ${{ inputs.dry_run || 'false' }}
```

## Inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `service-account-json` | Yes | — | Full JSON contents of the service-account key |
| `rules-path` | No | `play-pricing-rules.json` | Path to rules file, relative to repo root |
| `dry-run` | No | `false` | Log planned changes without modifying Play Console |

## Pricing rules format

```json
[
  {
    "packageName": "com.example.app",
    "sourceProductId": "premium_unlock",
    "targetProductId": "premium_unlock_sale",
    "discountPercent": 10
  },
  {
    "packageName": "com.example.app",
    "sourceProductId": "coins_small",
    "targetProductId": "coins_small_promo",
    "multiplier": 3,
    "rounding": "up"
  }
]
```

| Field | Type | Description |
|---|---|---|
| `packageName` | string | Application ID of the app the products belong to |
| `sourceProductId` | string | Product ID to read prices from |
| `targetProductId` | string | Product ID to update |
| `discountPercent` | number | Discount percentage (exclusive: 0–100). Mutually exclusive with `multiplier`. |
| `multiplier` | number | Factor applied to the source price (> 0). E.g. `3` = target is 3× the source, `0.5` = half. Mutually exclusive with `discountPercent`. |
| `rounding` | string | Optional. How the computed price snaps to the currency's minor unit: `nearest` (default), `up`, or `down`. |

Each rule must set **exactly one** of `discountPercent` or `multiplier`. `rounding` is optional and defaults to `nearest`.

## Custom rules file path

```yaml
- uses: Idlevelopment/play-discount-sync@v1
  with:
    service-account-json: ${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}
    rules-path: '.github/pricing/my-rules.json'
```

## Dry run

Run the workflow manually from **Actions → Update Google Play IAP Pricing → Run workflow** with **Dry run** checked. The action prints a full table of per-region prices without making any changes:

```
  Region         Source       Target       Chosen
  -------- ------------ ------------ ------------
  US               9.99         8.99         8.99
  GB               7.99         7.19         7.19
  JP            1480.00      1332.00      1332.00
  ...
```

Rows marked with `!` are regions where rounding to the currency unit moved the price off the exact computed target.

## Notes

- **No price tiers.** Google Play accepts arbitrary amounts, so prices are exact arithmetic rounded to each currency's minor unit. Zero-decimal currencies (JPY, KRW, …) are handled automatically.
- Only **regions the target product already sells in** are updated. A region present on the target but missing from the source is skipped (and reported). Existing **availability** and other per-region settings on the target are preserved.
- Each run writes the target product's purchase-option prices in a single `PATCH` (`updateMask=purchaseOptions`), reusing the regions version Google returns for the product.
- The service account needs permission to view financial data and manage in-app products for the app, and must be linked under **Play Console → Setup → API access**.
- **One-time products only.** Subscriptions use the `monetization.subscriptions` API and are not handled by this action.
