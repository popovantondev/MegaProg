import unittest
from src.calculator import multiply


class MultiplicationTests(unittest.TestCase):
    def test_multiplies_positive_and_negative_numbers(self):
        self.assertEqual(multiply(7, -2), -14)


if __name__ == '__main__':
    unittest.main()
