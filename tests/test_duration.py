import unittest
from giveaway.core import duration_seconds

class DurationTests(unittest.TestCase):
    def test_units(self):
        for amount,unit,expected in [(30,'seconds',30),(5,'minutes',300),(2,'hours',7200),(3,'days',259200)]:
            self.assertEqual(duration_seconds(amount,unit),expected)
    def test_limit_and_invalid_values(self):
        self.assertEqual(duration_seconds(365,'days'),31536000)
        for amount,unit in [(366,'days'),(0,'seconds'),(-1,'hours'),(1,'bad'),(1.5,'days'),(True,'days')]:
            with self.assertRaises(ValueError):duration_seconds(amount,unit)
