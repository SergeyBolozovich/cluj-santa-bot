import unittest
from validation import validate_title, parse_budget, format_budget


class ValidationTests(unittest.TestCase):
    def test_title_boundaries_and_trimming(self):
        self.assertEqual(validate_title("  Я  "), "Я")
        self.assertEqual(validate_title("Я" * 100), "Я" * 100)
        self.assertEqual(validate_title("<Санта & друзья>"), "<Санта & друзья>")
        for value in ("", "  \t\n", "Я" * 101):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_title(value)

    def test_budget_boundaries_and_decimal_separators(self):
        cases = {"0.01": 1, "0,01": 1, "100": 10000, "100.50": 10050,
                 "100,50": 10050, "100000": 10000000, "100000,00": 10000000,
                 " 1,2 ": 120, "0001.01": 101}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_budget(value), expected)

    def test_invalid_budgets_are_not_rounded(self):
        for value in ("", "0", "0.00", "-1", "+1", "100000.01", "100001", "0.001",
                      "1.234", "1,2.3", "1,000.00", "1 000", "1e2", "NaN", "inf",
                      "100 RON", ".50", "1.", "１２", "9" * 100):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_budget(value)

    def test_budget_display(self):
        self.assertEqual(format_budget(1), "0,01 RON")
        self.assertEqual(format_budget(10000000), "100000,00 RON")


if __name__ == "__main__":
    unittest.main()
