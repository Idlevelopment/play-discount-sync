#!/usr/bin/env python3
"""Google Play one-time IAP Pricing Updater

For each rule in the pricing rules file:
  For every region the target product is sold in:
    price(target, region) = price(source, region) * factor

where factor is either (1 - discountPercent / 100) or an explicit multiplier
(e.g. 3 to make the target 3x the source). Exactly one of the two fields must
be set per rule.

The factor is applied individually per region using the source product's actual
local price in each country. Unlike Apple, Google Play has no fixed price tiers:
any amount is allowed, so the computed price is simply rounded to the currency's
minor unit (e.g. cents for USD, whole yen for JPY). The target product's
purchase-option prices are updated in a single PATCH; availability and every
other field are preserved.

This targets the monetization.onetimeproducts API (one-time products only).
Subscriptions use a different API (monetization.subscriptions) and are not
handled here.

How to find a product ID:
  Play Console -> your app -> Monetize -> Products -> In-app products ->
  the "Product ID" column (a string like 'premium_unlock', not a number).

Required environment variables:
  GOOGLE_SERVICE_ACCOUNT_JSON — Full JSON contents of a service-account key with
                                access to the Google Play Android Developer API.

Optional:
  DRY_RUN     — Set to "true" to log actions without modifying anything.
  RULES_PATH  — Path to the pricing rules JSON file (default: play-pricing-rules.json).

The service account must be linked under Play Console -> Setup -> API access and
granted permission to view financial data / manage products for the app.
"""

import json
import os
import sys
from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path

import google.auth.transport.requests
import requests
from babel.numbers import get_currency_precision
from google.oauth2 import service_account

BASE_URL = "https://androidpublisher.googleapis.com/androidpublisher/v3"
SCOPE = "https://www.googleapis.com/auth/androidpublisher"

RULES_PATH = Path(os.environ.get("RULES_PATH", "play-pricing-rules.json"))

NANOS_PER_UNIT = 1_000_000_000


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def generate_token(sa_json: str) -> str:
    """Mint an OAuth2 access token from a service-account key JSON string."""
    info = json.loads(sa_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def api_get(url: str, hdrs: dict, params: dict | None = None) -> dict:
    resp = requests.get(url, headers=hdrs, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_product(hdrs: dict, package_name: str, product_id: str) -> dict:
    """Return the full OneTimeProduct resource for a product."""
    return api_get(
        f"{BASE_URL}/applications/{package_name}/oneTimeProducts/{product_id}", hdrs
    )


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

def money_to_amount(price: dict) -> Decimal:
    """Convert a {currencyCode, units, nanos} Money object to a Decimal amount.

    units is an int64 serialised as a string; nanos is an int in [0, 1e9).
    """
    units = Decimal(str(price.get("units", "0") or "0"))
    nanos = Decimal(int(price.get("nanos", 0) or 0))
    return units + nanos / NANOS_PER_UNIT


_ROUNDING_MODES = {
    "nearest": ROUND_HALF_UP,
    "up": ROUND_CEILING,
    "down": ROUND_FLOOR,
}


def amount_to_money(amount: Decimal, currency: str, rounding: str = "nearest") -> dict:
    """Round a Decimal amount to the currency's minor unit and return a Money object.

    nearest — round half up to the smallest currency unit (default).
    up      — round up (ceil) to the smallest currency unit.
    down    — round down (floor) to the smallest currency unit.
    """
    precision = get_currency_precision(currency)
    quantum = Decimal(1).scaleb(-precision)  # precision 2 -> 0.01, precision 0 -> 1
    rounded = amount.quantize(quantum, rounding=_ROUNDING_MODES[rounding])

    units = int(rounded)  # rounded is non-negative
    frac = rounded - units
    nanos = int((frac * NANOS_PER_UNIT).to_integral_value())
    return {"currencyCode": currency, "units": str(units), "nanos": nanos}


def scale_price(price: dict, factor: float, rounding: str) -> dict:
    """Multiply a Money object by factor, rounded to the currency's minor unit."""
    target = money_to_amount(price) * Decimal(str(factor))
    return amount_to_money(target, price["currencyCode"], rounding)


# ---------------------------------------------------------------------------
# Purchase options
# ---------------------------------------------------------------------------

def get_buy_option(product: dict) -> dict:
    """Return the first 'buy' purchase option of a one-time product.

    One-time products normally have exactly one buy option. Rent options are
    not supported by this tool.
    """
    for option in product.get("purchaseOptions", []):
        if "buyOption" in option:
            return option
    raise LookupError(
        f"Product {product.get('productId')!r} has no buy purchase option. "
        "Only one-time 'buy' products are supported."
    )


def source_price_map(product: dict) -> dict[str, dict]:
    """Return {regionCode: Money} from a source product's buy option."""
    option = get_buy_option(product)
    prices: dict[str, dict] = {}
    for cfg in option.get("regionalPricingAndAvailabilityConfigs", []):
        region = cfg.get("regionCode")
        price = cfg.get("price")
        if region and price:
            prices[region] = price
    if not prices:
        raise LookupError(
            f"No regional prices found for source product {product.get('productId')!r}."
        )
    return prices


# ---------------------------------------------------------------------------
# Rule processing
# ---------------------------------------------------------------------------

VALID_ROUNDING = ("nearest", "up", "down")


def resolve_multiplier(rule: dict) -> tuple[float, str]:
    """Return (multiplier, label) from a rule's discountPercent or multiplier.

    Exactly one of the two fields must be present. The multiplier is the factor
    applied to the source price: target = source * multiplier.
    """
    has_discount = "discountPercent" in rule
    has_multiplier = "multiplier" in rule

    if has_discount and has_multiplier:
        raise ValueError(
            "Specify either 'discountPercent' or 'multiplier', not both."
        )
    if not has_discount and not has_multiplier:
        raise ValueError(
            "Each rule must specify either 'discountPercent' or 'multiplier'."
        )

    if has_discount:
        discount = rule["discountPercent"]
        if not (0 < discount < 100):
            raise ValueError(
                f"discountPercent must be between 0 and 100 (exclusive), got {discount}"
            )
        return 1 - discount / 100, f"discount={discount}%"

    multiplier = rule["multiplier"]
    if multiplier <= 0:
        raise ValueError(f"multiplier must be greater than 0, got {multiplier}")
    return multiplier, f"multiplier={multiplier}"


def resolve_rounding(rule: dict) -> str:
    """Return the rounding strategy for a rule (default 'nearest')."""
    rounding = rule.get("rounding", "nearest")
    if rounding not in VALID_ROUNDING:
        raise ValueError(
            f"rounding must be one of {VALID_ROUNDING}, got {rounding!r}"
        )
    return rounding


def regions_version_of(product: dict) -> str:
    """Return the regions-version string Google used for this product.

    Required (and must be non-empty) when patching; reusing the value from the
    product's own get response avoids hardcoding it.
    """
    version = product.get("regionsVersion", {}).get("version")
    if not version:
        raise LookupError(
            f"No regionsVersion found for product {product.get('productId')!r}; "
            "cannot patch without it."
        )
    return version


# ---------------------------------------------------------------------------
# Patch
# ---------------------------------------------------------------------------

def apply_prices(
    hdrs: dict,
    product: dict,
    package_name: str,
    product_id: str,
    regions_version: str,
    dry_run: bool,
) -> None:
    """PATCH the target product's purchase options with the updated prices.

    Endpoint: PATCH .../onetimeproducts/{productId}?updateMask=purchaseOptions
    Note: GET uses camelCase 'oneTimeProducts', but PATCH uses lowercase
    'onetimeproducts' — Google's API is inconsistent here (per the v3
    discovery doc). Do not "fix" the casing to match.
    Sends the full (modified) product; updateMask limits the change to the
    purchase-option array, so availability and other fields are preserved.
    """
    if dry_run:
        print("  [DRY RUN] Would PATCH "
              f"/onetimeproducts/{product_id} (updateMask=purchaseOptions)")
        return

    params = {
        "updateMask": "purchaseOptions",
        "regionsVersion.version": regions_version,
    }
    resp = requests.patch(
        f"{BASE_URL}/applications/{package_name}/onetimeproducts/{product_id}",
        headers=hdrs,
        params=params,
        json=product,
        timeout=60,
    )
    if not resp.ok:
        print(f"  API error {resp.status_code}: {resp.text}", file=sys.stderr)
    resp.raise_for_status()


def process_rule(hdrs: dict, rule: dict, dry_run: bool) -> None:
    package_name = rule["packageName"]
    src_id = rule["sourceProductId"]
    tgt_id = rule["targetProductId"]
    multiplier, label = resolve_multiplier(rule)
    rounding = resolve_rounding(rule)

    print(f"\nRule: [{package_name}] {src_id} -> {tgt_id}  {label}  rounding={rounding}")

    # --- Source: read prices for every region it is sold in ---
    src_product = get_product(hdrs, package_name, src_id)
    src_prices = source_price_map(src_product)
    print(f"  Source regions: {len(src_prices)}")

    # --- Target: read product, update each region's price in place ---
    tgt_product = get_product(hdrs, package_name, tgt_id)
    regions_version = regions_version_of(tgt_product)
    tgt_option = get_buy_option(tgt_product)

    price_log: list[tuple] = []  # (region, src_amount, target_amount, chosen_amount)
    skipped: list[str] = []
    updated = 0

    for cfg in tgt_option.get("regionalPricingAndAvailabilityConfigs", []):
        region = cfg.get("regionCode")
        if region not in src_prices:
            skipped.append(region)
            continue

        src_price = src_prices[region]
        src_amount = money_to_amount(src_price)
        target_amount = src_amount * Decimal(str(multiplier))
        new_price = scale_price(src_price, multiplier, rounding)
        chosen_amount = money_to_amount(new_price)

        cfg["price"] = new_price
        updated += 1
        price_log.append((region, src_amount, target_amount, chosen_amount))

    if skipped:
        print(
            f"  WARNING: source has no price for {len(skipped)} target region(s) "
            f"(skipped): {', '.join(skipped[:10])}{'...' if len(skipped) > 10 else ''}",
            file=sys.stderr,
        )

    if dry_run:
        print(f"\n  {'Region':<8} {'Source':>12} {'Target':>12} {'Chosen':>12}")
        print(f"  {'-' * 8} {'-' * 12} {'-' * 12} {'-' * 12}")
        for region, src_amount, target_amount, chosen_amount in price_log:
            flag = " !" if abs(chosen_amount - target_amount) > Decimal("0.005") else ""
            print(f"  {region:<8} {src_amount:>12.2f} {target_amount:>12.2f} "
                  f"{chosen_amount:>12.2f}{flag}")
        print()

    print(f"  Applying prices for {updated} region(s)...")
    apply_prices(hdrs, tgt_product, package_name, tgt_id, regions_version, dry_run)

    suffix = " [DRY RUN]" if dry_run else ""
    print(f"  ✓ Product {tgt_id} updated for {updated} region(s){suffix}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    sa_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"

    with open(RULES_PATH) as f:
        rules: list[dict] = json.load(f)

    if not rules:
        print("No pricing rules defined. Add entries to your pricing rules file.")
        return

    if dry_run:
        print("=== DRY RUN mode — no changes will be made ===\n")

    token = generate_token(sa_json)
    hdrs = auth_headers(token)

    errors: list[str] = []
    for rule in rules:
        try:
            process_rule(hdrs, rule, dry_run)
        except Exception as exc:
            msg = (f"FAILED [{rule.get('sourceProductId')} -> "
                   f"{rule.get('targetProductId')}]: {exc}")
            print(f"\nERROR: {msg}", file=sys.stderr)
            errors.append(msg)

    print()
    if errors:
        print(f"{len(errors)} rule(s) failed:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    mode = "DRY RUN — " if dry_run else ""
    print(f"{mode}All {len(rules)} rule(s) applied successfully.")


if __name__ == "__main__":
    main()
