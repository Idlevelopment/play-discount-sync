#!/usr/bin/env python3
"""Unit tests for the pure pricing logic in update_pricing.

Run with:  python3 -m unittest discover -s tests -p 'test_*.py'
"""

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import update_pricing as up


def money(currency: str, units, nanos: int = 0) -> dict:
    return {"currencyCode": currency, "units": str(units), "nanos": nanos}


class ResolveMultiplierTest(unittest.TestCase):
    # --- discountPercent path ---

    def test_discount_returns_factor_below_one(self):
        factor, label = up.resolve_multiplier({"discountPercent": 10})
        self.assertAlmostEqual(factor, 0.9)
        self.assertIn("discount=10%", label)

    def test_discount_applied_to_price(self):
        factor, _ = up.resolve_multiplier({"discountPercent": 25})
        self.assertAlmostEqual(round(100 * factor, 2), 75.0)

    def test_discount_66_67_yields_third(self):
        factor, _ = up.resolve_multiplier({"discountPercent": 66.6667})
        self.assertAlmostEqual(99 * factor, 33.0, places=2)

    def test_discount_zero_rejected(self):
        with self.assertRaises(ValueError):
            up.resolve_multiplier({"discountPercent": 0})

    def test_discount_hundred_rejected(self):
        with self.assertRaises(ValueError):
            up.resolve_multiplier({"discountPercent": 100})

    def test_discount_negative_rejected(self):
        with self.assertRaises(ValueError):
            up.resolve_multiplier({"discountPercent": -5})

    # --- multiplier path ---

    def test_multiplier_returns_factor(self):
        factor, label = up.resolve_multiplier({"multiplier": 3})
        self.assertEqual(factor, 3)
        self.assertIn("multiplier=3", label)

    def test_multiplier_3x_applied_to_price(self):
        factor, _ = up.resolve_multiplier({"multiplier": 3})
        self.assertAlmostEqual(round(9.99 * factor, 2), 29.97)

    def test_multiplier_fractional(self):
        factor, _ = up.resolve_multiplier({"multiplier": 0.5})
        self.assertAlmostEqual(round(10 * factor, 2), 5.0)

    def test_multiplier_zero_rejected(self):
        with self.assertRaises(ValueError):
            up.resolve_multiplier({"multiplier": 0})

    def test_multiplier_negative_rejected(self):
        with self.assertRaises(ValueError):
            up.resolve_multiplier({"multiplier": -1})

    # --- mutual exclusivity ---

    def test_both_fields_rejected(self):
        with self.assertRaises(ValueError):
            up.resolve_multiplier({"discountPercent": 10, "multiplier": 3})

    def test_neither_field_rejected(self):
        with self.assertRaises(ValueError):
            up.resolve_multiplier({})


class ResolveRoundingTest(unittest.TestCase):
    def test_defaults_to_nearest_when_omitted(self):
        self.assertEqual(up.resolve_rounding({}), "nearest")

    def test_each_valid_value_returned(self):
        for value in ("nearest", "up", "down"):
            self.assertEqual(up.resolve_rounding({"rounding": value}), value)

    def test_unknown_value_rejected(self):
        with self.assertRaises(ValueError):
            up.resolve_rounding({"rounding": "ceil"})


class MoneyToAmountTest(unittest.TestCase):
    def test_units_and_nanos(self):
        self.assertEqual(up.money_to_amount(money("USD", 19, 990000000)), Decimal("19.99"))

    def test_whole_units(self):
        self.assertEqual(up.money_to_amount(money("JPY", 1000, 0)), Decimal("1000"))

    def test_zero(self):
        self.assertEqual(up.money_to_amount(money("USD", 0, 0)), Decimal("0"))

    def test_string_units_int64(self):
        # int64 fields come back as strings from the API
        self.assertEqual(up.money_to_amount(money("USD", "5", 500000000)), Decimal("5.5"))

    def test_missing_fields_default_to_zero(self):
        self.assertEqual(up.money_to_amount({"currencyCode": "USD"}), Decimal("0"))


class AmountToMoneyTest(unittest.TestCase):
    def test_two_decimal_currency(self):
        m = up.amount_to_money(Decimal("17.991"), "USD")
        self.assertEqual(m, money("USD", 17, 990000000))

    def test_zero_decimal_currency_rounds_to_whole(self):
        m = up.amount_to_money(Decimal("899.99"), "JPY")
        self.assertEqual(m, money("JPY", 900, 0))

    def test_three_decimal_currency(self):
        # BHD has 3 decimal places
        m = up.amount_to_money(Decimal("1.2344"), "BHD")
        self.assertEqual(m, money("BHD", 1, 234000000))

    def test_nearest_half_rounds_up(self):
        self.assertEqual(up.amount_to_money(Decimal("1.005"), "USD"),
                         money("USD", 1, 10000000))

    def test_up_ceils_to_minor_unit(self):
        self.assertEqual(up.amount_to_money(Decimal("1.001"), "USD", "up"),
                         money("USD", 1, 10000000))

    def test_down_floors_to_minor_unit(self):
        self.assertEqual(up.amount_to_money(Decimal("1.009"), "USD", "down"),
                         money("USD", 1, 0))


class ScalePriceTest(unittest.TestCase):
    def test_ten_percent_discount(self):
        # 19.99 * 0.9 = 17.991 -> 17.99
        out = up.scale_price(money("USD", 19, 990000000), 0.9, "nearest")
        self.assertEqual(out, money("USD", 17, 990000000))

    def test_three_x_multiplier(self):
        # 9.99 * 3 = 29.97
        out = up.scale_price(money("USD", 9, 990000000), 3, "nearest")
        self.assertEqual(out, money("USD", 29, 970000000))

    def test_zero_decimal_currency(self):
        # JPY 1000 * 0.9 = 900, no fractional yen
        out = up.scale_price(money("JPY", 1000, 0), 0.9, "nearest")
        self.assertEqual(out, money("JPY", 900, 0))

    def test_rounding_up_vs_down(self):
        # 10.00 * 0.729 = 7.29 exactly... use a value that lands between cents
        src = money("USD", 10, 0)
        # 10 * 0.7251 = 7.251 -> up 7.26, down 7.25, nearest 7.25
        self.assertEqual(up.scale_price(src, 0.7251, "up"), money("USD", 7, 260000000))
        self.assertEqual(up.scale_price(src, 0.7251, "down"), money("USD", 7, 250000000))
        self.assertEqual(up.scale_price(src, 0.7251, "nearest"), money("USD", 7, 250000000))

    def test_preserves_currency(self):
        out = up.scale_price(money("GBP", 7, 990000000), 0.5, "nearest")
        self.assertEqual(out["currencyCode"], "GBP")


class GetBuyOptionTest(unittest.TestCase):
    def test_returns_buy_option(self):
        product = {
            "productId": "p",
            "purchaseOptions": [
                {"purchaseOptionId": "rent", "rentOption": {}},
                {"purchaseOptionId": "buy", "buyOption": {}},
            ],
        }
        self.assertEqual(up.get_buy_option(product)["purchaseOptionId"], "buy")

    def test_raises_without_buy_option(self):
        product = {"productId": "p", "purchaseOptions": [{"rentOption": {}}]}
        with self.assertRaises(LookupError):
            up.get_buy_option(product)


class SourcePriceMapTest(unittest.TestCase):
    def test_builds_region_map(self):
        product = {
            "productId": "p",
            "purchaseOptions": [
                {
                    "buyOption": {},
                    "regionalPricingAndAvailabilityConfigs": [
                        {"regionCode": "US", "price": money("USD", 9, 990000000)},
                        {"regionCode": "GB", "price": money("GBP", 7, 990000000)},
                    ],
                }
            ],
        }
        prices = up.source_price_map(product)
        self.assertEqual(set(prices), {"US", "GB"})
        self.assertEqual(prices["US"]["currencyCode"], "USD")

    def test_raises_when_no_prices(self):
        product = {
            "productId": "p",
            "purchaseOptions": [
                {"buyOption": {}, "regionalPricingAndAvailabilityConfigs": []}
            ],
        }
        with self.assertRaises(LookupError):
            up.source_price_map(product)


class RegionsVersionTest(unittest.TestCase):
    def test_returns_version(self):
        self.assertEqual(
            up.regions_version_of({"productId": "p", "regionsVersion": {"version": "2022/02"}}),
            "2022/02",
        )

    def test_raises_when_missing(self):
        with self.assertRaises(LookupError):
            up.regions_version_of({"productId": "p"})


if __name__ == "__main__":
    unittest.main()
