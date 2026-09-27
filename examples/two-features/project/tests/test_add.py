import unittest
from src.calculator import add


class AdditionTests(unittest.TestCase):
    def test_adds_positive_and_negative_numbers(self):
        self.assertEqual(add(7, -2), 5)


if __name__ == '__main__':
    unittest.main()
