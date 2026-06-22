"""Tests for value_coercion: HA template result -> XSIG join value."""

import unittest

from loader import load

vc = load("value_coercion")


class ToDigitalTests(unittest.TestCase):
    def test_bool_passthrough(self):
        self.assertIs(vc.to_digital(True), True)
        self.assertIs(vc.to_digital(False), False)

    def test_true_strings(self):
        for s in ("on", "True", "true", "1"):
            self.assertIs(vc.to_digital(s), True, s)

    def test_false_strings(self):
        for s in ("off", "False", "false", "0"):
            self.assertIs(vc.to_digital(s), False, s)

    def test_numeric_inputs(self):
        # Non-bool ints are stringified: 1 -> "1" -> True, 0 -> "0" -> False.
        self.assertIs(vc.to_digital(1), True)
        self.assertIs(vc.to_digital(0), False)

    def test_uninterpretable_returns_none(self):
        for s in ("unknown", "unavailable", "", "maybe", "2", "ON"):
            self.assertIsNone(vc.to_digital(s), s)


class ToAnalogTests(unittest.TestCase):
    def test_int_and_float_strings(self):
        self.assertEqual(vc.to_analog("123"), 123)
        self.assertEqual(vc.to_analog("740"), 740)
        # int(float(...)) truncates toward zero.
        self.assertEqual(vc.to_analog("12.9"), 12)
        self.assertEqual(vc.to_analog(740), 740)

    def test_clamps_to_uint16(self):
        self.assertEqual(vc.to_analog("70000"), 65535)
        self.assertEqual(vc.to_analog("65535"), 65535)
        self.assertEqual(vc.to_analog("-5"), 0)
        self.assertEqual(vc.to_analog("0"), 0)

    def test_invalid_returns_none(self):
        for s in ("unknown", "unavailable", "None", "none", "", "abc", "1,5"):
            self.assertIsNone(vc.to_analog(s), s)


class ToSerialTests(unittest.TestCase):
    def test_passthrough(self):
        self.assertEqual(vc.to_serial("hello"), "hello")
        self.assertEqual(vc.to_serial("你好"), "你好")
        self.assertEqual(vc.to_serial(123), "123")

    def test_invalid_returns_none(self):
        for s in ("unknown", "unavailable", "None", "none", ""):
            self.assertIsNone(vc.to_serial(s), s)


if __name__ == "__main__":
    unittest.main()
