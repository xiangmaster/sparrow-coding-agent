"""配送费规则的回归测试。"""

import unittest

from delivery import calculate_delivery_fee


class DeliveryFeeTests(unittest.TestCase):
    def test_regular_user_reaches_free_shipping_threshold(self) -> None:
        self.assertEqual(calculate_delivery_fee(99, 1), 0)

    def test_member_reaches_lower_free_shipping_threshold(self) -> None:
        self.assertEqual(calculate_delivery_fee(69, 3, is_member=True), 0)

    def test_amount_below_threshold_is_not_free(self) -> None:
        self.assertEqual(calculate_delivery_fee(98.99, 1), 8)

    def test_started_kilogram_is_charged(self) -> None:
        self.assertEqual(calculate_delivery_fee(20, 1.01), 10)
        self.assertEqual(calculate_delivery_fee(20, 2.2), 12)

    def test_exact_weight_boundary(self) -> None:
        self.assertEqual(calculate_delivery_fee(20, 1), 8)
        self.assertEqual(calculate_delivery_fee(20, 2), 10)

    def test_invalid_inputs_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            calculate_delivery_fee(-0.01, 1)
        with self.assertRaises(ValueError):
            calculate_delivery_fee(10, 0)


if __name__ == "__main__":
    unittest.main()
