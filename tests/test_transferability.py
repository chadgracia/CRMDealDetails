"""Transferability on the deal page + Explore Similar Companies (fixtures only).
lambda_function.py reads S3 at import, so the needed functions are extracted with ast."""
import ast
import json
import logging
import os
import time
import types
import unittest
import urllib.parse
import urllib.request

SRC = os.path.join(os.path.dirname(__file__), "..", "lambda_function.py")
WANT_PREFIXES = ("TR_", "_TR_", "SIMILAR_")
WANT_FUNCS = {
    "_tr_ids", "_tr_parse_field_labels", "_tr_pipeline_get", "_tr_lookup_field_from_pipeline",
    "_tr_read_field_cache", "transferability_field", "transferability_labels", "tr_is_direct_sell",
    "tr_deal_unavailable", "tr_deal_notes", "_tr_s3_json", "_tr_deal_company_id",
    "transferability_companies", "tr_unavailable_ids_from_deals", "transferability_unavailable_ids",
    "tr_filter_deals", "tr_page_state", "render_tr_unavailable_status", "render_tr_notes",
    "_sim_esc", "_sim_tags", "_sim_load_deals", "compute_similar_companies", "_sim_num", "_sim_span",
    "_sim_rank_map", "_sim_price", "_sim_pick_deal", "render_similar_companies",
}


def load():
    tree = ast.parse(open(SRC).read())
    body = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in WANT_FUNCS:
            body.append(n)
        elif isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id.startswith(WANT_PREFIXES) for t in n.targets):
            body.append(n)
    ns = {"json": json, "time": time, "urllib": urllib, "logger": logging.getLogger("t"),
          "boto3": types.SimpleNamespace(client=lambda *a, **k: None)}
    exec(compile(ast.Module(body=body, type_ignores=[]), SRC, "exec"), ns)
    return ns


FK = "custom_label_3900670"
FIELD = {"field_id": 3900670, "field_key": FK, "resolved_at": 1e12,
         "options": {"Direct Allowed": 1, "Only Preferred": 2, "Direct Blocked": 3,
                     "Only Approved Transactors": 4, "Only Current Cap Table": 5}}
HADRIAN_CF = {FK: [2]}
COMMON_DIRECT_SELL = {"custom_label_1958": 5011675, "custom_label_3064360": 6250090,
                      "custom_label_3064330": 5077831}
BUY = {"custom_label_1958": 5077819, "custom_label_3064360": 6250090}


class DealPage(unittest.TestCase):
    def setUp(self):
        self.ns = load()
        self.ns["transferability_field"] = lambda *a, **k: FIELD

    def test_hadrian_sell_unavailable(self):
        unavail, notes = self.ns["tr_page_state"](COMMON_DIRECT_SELL, HADRIAN_CF, False)
        self.assertTrue(unavail)
        self.assertEqual(notes, [])
        self.assertIn("Not currently available — issuer transfer restrictions",
                      self.ns["render_tr_unavailable_status"]())

    def test_hadrian_buy_note(self):
        unavail, notes = self.ns["tr_page_state"](BUY, HADRIAN_CF, True)
        self.assertFalse(unavail)
        self.assertIn("Issuer currently allows transfers of preferred shares only.",
                      self.ns["render_tr_notes"](notes))

    def test_fail_open(self):
        self.ns["transferability_field"] = lambda *a, **k: None
        self.assertEqual(self.ns["tr_page_state"](COMMON_DIRECT_SELL, HADRIAN_CF, False), (False, []))
        self.assertEqual(self.ns["render_tr_notes"]([]), "")


class Similar(unittest.TestCase):
    def test_hidden_deal_leaves_chips_and_counts(self):
        ns = load()
        rows = [
            {"id": 1, "company": "Anduril", "type": "Sell Order", "company_industry": "Defense, Manufacturing"},
            {"id": 55718581, "company": "Hadrian", "type": "Sell Order", "company_industry": "Defense, Manufacturing"},
            {"id": 3, "company": "Saronic", "type": "Sell Order", "company_industry": "Defense"},
            {"id": 4, "company": "Saronic", "type": "Sell Order", "company_industry": "Defense"},
        ]

        class S3:
            def get_object(self, Bucket, Key):
                import io
                return {"Body": io.BytesIO(json.dumps(rows).encode())}
        ns["boto3"].client = lambda *a, **k: S3()
        ns["transferability_unavailable_ids"] = lambda *a, **k: {"55718581", "4"}
        html = ns["render_similar_companies"]("Anduril", "Sell Order", 1)
        self.assertNotIn("Hadrian", html)
        self.assertIn('Saronic<sup class="similar-count">1</sup>', html)

        ns["transferability_unavailable_ids"] = lambda *a, **k: set()
        html = ns["render_similar_companies"]("Anduril", "Sell Order", 1)
        self.assertIn('Hadrian<sup class="similar-count">1</sup>', html)
        self.assertIn('Saronic<sup class="similar-count">2</sup>', html)


if __name__ == "__main__":
    unittest.main()
