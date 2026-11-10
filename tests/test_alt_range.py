"""Size range text in the "Other live offers" box (alt_live_link_text)."""
import ast
import os
import unittest

SRC = os.path.join(os.path.dirname(__file__), "..", "lambda_function.py")
FUNCS = {"alt_live_link_text", "_alt_num", "_seo_money", "_alt_struct_kind"}


def load():
    tree = ast.parse(open(SRC).read())
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in FUNCS]
    ns = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), SRC, "exec"), ns)
    return ns["alt_live_link_text"]


class AltRange(unittest.TestCase):
    def setUp(self):
        self.f = load()

    def deal(self, mn, mx):
        return {"type": "Sell Order", "min_deal_size": mn, "max_deal_size": mx, "structure": "Fund"}

    def test_equal(self):
        self.assertEqual(self.f(self.deal(1000000, 1000000)), "Sell · $1M · SPV")

    def test_equal_after_formatting(self):
        self.assertEqual(self.f(self.deal(1000000, 1004000)), "Sell · $1M · SPV")

    def test_min_only(self):
        self.assertEqual(self.f(self.deal(500000, None)), "Sell · from $500K · SPV")

    def test_max_only(self):
        self.assertEqual(self.f(self.deal("", "$2,000,000")), "Sell · up to $2M · SPV")

    def test_normal_range(self):
        self.assertEqual(self.f(self.deal(1000000, 5000000)), "Sell · $1M–$5M · SPV")

    def test_none(self):
        self.assertEqual(self.f(self.deal(None, None)), "Sell · SPV")


if __name__ == "__main__":
    unittest.main()
