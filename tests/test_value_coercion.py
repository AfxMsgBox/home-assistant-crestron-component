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


class ResolveJoinWriteTests(unittest.TestCase):
    """The to_joins write path (parse key + coerce value) used by ToJoinBridge."""

    def test_digital(self):
        self.assertEqual(vc.resolve_join_write("d12", "on"), ("d", 12, True))
        self.assertEqual(vc.resolve_join_write("d12", "off"), ("d", 12, False))

    def test_analog_clamped(self):
        self.assertEqual(vc.resolve_join_write("a3", "70000"), ("a", 3, 65535))
        self.assertEqual(vc.resolve_join_write("a3", "24"), ("a", 3, 24))

    def test_serial(self):
        self.assertEqual(vc.resolve_join_write("s4", "你好"), ("s", 4, "你好"))

    def test_unknown_value_returns_none(self):
        # Unknown/garbage values must not be written.
        self.assertIsNone(vc.resolve_join_write("a3", "unavailable"))
        self.assertIsNone(vc.resolve_join_write("d3", "garbage"))
        self.assertIsNone(vc.resolve_join_write("s3", "unknown"))

    def test_unknown_kind_returns_none(self):
        self.assertIsNone(vc.resolve_join_write("x9", "1"))

    def test_malformed_number_raises(self):
        # Caller logs this distinctly (a misconfigured join key).
        with self.assertRaises(ValueError):
            vc.resolve_join_write("dabc", "1")


if __name__ == "__main__":
    unittest.main()
