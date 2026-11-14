"""Deal Visibility tests: deal page (Private / Interested / All / blank) and the
other-deal list filter (deals.json + visibility-overrides.json). Stub S3 + stub
HTTP only (reuses the Partner Desk stubs); run with
python3.12 -m unittest discover -s tests"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_partner_desk as pd  # noqa: E402  (installs the stubs, imports lambda_function)

lf = pd.lf
VF = lf.VISIBILITY_FIELD
DEALS = ("full-pipeline-cache", "deals.json")
OVR = ("full-pipeline-cache", "visibility-overrides.json")
PUBLIC = ("pipeline-public-deal-data", "pipeline_deals.json")


def reset(deals=None, overrides=None, public=None):
    for k in (DEALS, OVR, PUBLIC):
        pd.FakeS3.objects.pop(k, None)
    if deals is not None:
        pd.FakeS3.objects[DEALS] = {"deals": deals}
    if overrides is not None:
        pd.FakeS3.objects[OVR] = overrides
    if public is not None:
        pd.FakeS3.objects[PUBLIC] = public
    for c in (lf._TR_DEALS_CACHE, lf._VIS_OVERRIDES_CACHE, lf._TR_UNAVAILABLE_CACHE):
        c.update(ts=0.0, val=None)


class Normalise(unittest.TestCase):
    def test_values(self):
        cases = [
            (None, lf.VIS_ALL), ("", lf.VIS_ALL), ([], lf.VIS_ALL), ("abc", lf.VIS_ALL),
            (123, lf.VIS_ALL), (True, lf.VIS_ALL), ({"x": 1}, lf.VIS_ALL),
            (7223456, lf.VIS_ALL), (7223457, lf.VIS_INTERESTED), (7223458, lf.VIS_PRIVATE),
            ("7223458", lf.VIS_PRIVATE), (" 7223457 ", lf.VIS_INTERESTED),
            ("7223458.0", lf.VIS_PRIVATE), ([7223457], lf.VIS_INTERESTED), (["7223458"], lf.VIS_PRIVATE),
        ]
        for raw, want in cases:
            self.assertEqual(lf.vis_value(raw), want, raw)

    def test_deal_visibility(self):
        self.assertEqual(lf.deal_visibility({}), lf.VIS_ALL)
        self.assertEqual(lf.deal_visibility(None), lf.VIS_ALL)
        self.assertEqual(lf.deal_visibility({VF: 7223458}), lf.VIS_PRIVATE)


ROWS = [{"id": i, "company": c, "type": "Sell Order", "stage": "Firm",
         "company_industry": "Defense"} for i, c in
        ((1, "Anduril"), (2, "Saronic"), (3, "Hadrian"), (4, "Shield AI"), (5, "Epirus"))]
SNAP = [{"id": 1, "custom_fields": {}},
        {"id": 2, "custom_fields": {VF: 7223456}},
        {"id": 3, "custom_fields": {VF: 7223458}},
        {"id": 4, "custom_fields": {VF: [7223457]}},
        {"id": 5, "custom_fields": {VF: None}}]


class ListFilter(unittest.TestCase):
    def tearDown(self):
        reset()

    def ids(self):
        return [d["id"] for d in lf.tr_filter_deals(copy.deepcopy(ROWS))]

    def test_private_and_interested_dropped(self):
        reset(deals=SNAP)  # no overrides file (NoSuchKey) = no overrides
        self.assertEqual(self.ids(), [1, 2, 5])

    def test_override_wins(self):
        reset(deals=SNAP, overrides={"3": {"v": 7223456, "at": "2026-11-14T12:00:00Z"},
                                     "5": {"v": "7223458", "at": "2026-11-14T12:00:00Z"},
                                     "1": {"v": [7223457], "at": "2026-11-14T12:00:00Z"}})
        self.assertEqual(self.ids(), [2, 3])

    def test_map(self):
        m = lf.visibility_map(SNAP, {"99": 7223458, "2": lf.VIS_PRIVATE})
        self.assertEqual(m["1"], lf.VIS_ALL)
        self.assertEqual(m["2"], lf.VIS_PRIVATE)
        self.assertEqual(m["4"], lf.VIS_INTERESTED)
        self.assertEqual(m["99"], lf.VIS_PRIVATE)

    def test_malformed_overrides_ignored(self):
        reset(deals=SNAP, overrides=["junk"])
        self.assertEqual(self.ids(), [1, 2, 5])

    def test_overrides_cached_60s(self):
        reset(deals=[], overrides={"1": {"v": 7223458, "at": "x"}})
        self.assertEqual(lf.visibility_overrides(now=1000.0), {"1": lf.VIS_PRIVATE})
        pd.FakeS3.objects[OVR] = {}
        self.assertEqual(lf.visibility_overrides(now=1059.0), {"1": lf.VIS_PRIVATE})
        self.assertEqual(lf.visibility_overrides(now=1061.0), {})

    def test_fail_soft_without_deals_json(self):
        reset()  # deals.json missing -> lookup fails -> unfiltered, one log line
        with self.assertLogs(level="ERROR") as cm:
            self.assertEqual(self.ids(), [1, 2, 3, 4, 5])
        self.assertTrue(any("visibility: lookup failed; no visibility filtering" in m for m in cm.output))

    def test_similar_pills_exclude_hidden(self):
        reset(deals=SNAP, public=ROWS)
        html = lf.render_similar_companies("Anduril", "Sell Order", 1)
        self.assertIn("Saronic", html)
        self.assertIn("Epirus", html)
        self.assertNotIn("Hadrian", html)
        self.assertNotIn("Shield AI", html)

    def test_other_live_offers_exclude_hidden(self):
        rows = [dict(r, company="Anduril") for r in ROWS]
        reset(deals=SNAP, public=rows)
        alts = lf.live_alternatives(lf._sim_load_deals(), 999, "Anduril", "Sell Order", "", None, None)
        self.assertEqual(sorted(d["id"] for d in alts), [1, 2, 5])


class DealPage(unittest.TestCase):
    def setUp(self):
        self._cf = pd.DEAL["custom_fields"]
        reset()

    def tearDown(self):
        pd.DEAL["custom_fields"] = self._cf
        reset()

    def render(self, vis, admin):
        pd.DEAL["custom_fields"] = {} if vis is None else {VF: vis,
                                                           "custom_label_3064352": "12.34"}
        qs = {"deal_id": "999"}
        if admin:
            qs["admin_key"] = pd.ADMIN
        r = lf.lambda_handler({"queryStringParameters": qs}, None)
        self.assertEqual(r["statusCode"], 200)
        return r["body"]

    def test_private_public_stub(self):
        body = self.render(7223458, admin=False)
        self.assertIn('<meta name="robots" content="noindex">', body)
        self.assertIn("This deal is not currently available.", body)
        self.assertIn("<strong>Acme</strong>", body)
        self.assertIn("Search similar opportunities", body)
        self.assertIn("View live indications", body)
        self.assertIn('id="dealSwitcherModal"', body)
        for leak in ("Deal ID", "Acme Sell", "qa-box", "qa=submit", "bid-modal", "LOI",
                     "SPV Details", "Price", "12.34", "Private: not shown", "disclaimer"):
            self.assertNotIn(leak, body, leak)

    def test_private_admin_full_page(self):
        body = self.render("7223458.0", admin=True)
        self.assertIn("Private: not shown to counterparties", body)
        self.assertIn("Deal ID: 999", body)
        self.assertIn('<meta name="robots" content="noindex">', body)
        self.assertNotIn("This deal is not currently available.", body)

    def test_interested(self):
        pub = self.render([7223457], admin=False)
        self.assertIn('<meta name="robots" content="noindex">', pub)
        self.assertIn("Deal ID: 999", pub)
        self.assertNotIn("Interested investors only", pub)
        adm = self.render(7223457, admin=True)
        self.assertIn("Interested investors only", adm)
        self.assertIn('<meta name="robots" content="noindex">', adm)

    def test_all_and_blank_unchanged(self):
        for vis in (7223456, None):
            for admin in (False, True):
                body = self.render(vis, admin)
                self.assertNotIn("noindex", body)
                self.assertNotIn("vis-banner", body)
                self.assertIn("Deal ID: 999", body)


if __name__ == "__main__":
    unittest.main()
