"""Tests for the schema.join_key validator and numeric join validators."""

import unittest

from loader import load

try:
    import voluptuous as vol  # noqa: F401

    HAVE_VOL = True
except ImportError:
    HAVE_VOL = False


@unittest.skipUnless(HAVE_VOL, "voluptuous not installed")
class JoinKeyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = load("schema")

    def test_valid_keys(self):
        for v in ("d1", "d4096", "a1", "a1024", "s1", "s1024", "d12", "a35", "s4"):
            self.assertEqual(self.schema.join_key(v), v, v)

    def test_invalid_format(self):
        import voluptuous as vol

        for v in ("x1", "1d", "d", "", "d 1", " d1", "d1 ", "+1", "d01", "a+5", "dd1"):
            with self.assertRaises(vol.Invalid, msg=v):
                self.schema.join_key(v)

    def test_out_of_range(self):
        import voluptuous as vol

        for v in ("d0", "a0", "s0", "d4097", "a1025", "s1025", "d99999"):
            with self.assertRaises(vol.Invalid, msg=v):
                self.schema.join_key(v)

    def test_non_string_rejected(self):
        import voluptuous as vol

        for v in (5, None, 12, ["d1"]):
            with self.assertRaises(vol.Invalid):
                self.schema.join_key(v)

    def test_numeric_join_validators(self):
        import voluptuous as vol

        self.assertEqual(self.schema.digital_join(4096), 4096)
        self.assertEqual(self.schema.analog_join(1024), 1024)
        self.assertEqual(self.schema.serial_join(1), 1)
        for bad in (0, 4097):
            with self.assertRaises(vol.Invalid):
                self.schema.digital_join(bad)
        with self.assertRaises(vol.Invalid):
            self.schema.analog_join(1025)
        with self.assertRaises(vol.Invalid):
            self.schema.serial_join(1025)


if __name__ == "__main__":
    unittest.main()
