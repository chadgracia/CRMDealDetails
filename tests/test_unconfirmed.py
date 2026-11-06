"""Unconfirmed notice tests. Reuses the Partner Desk stubs (S3 + HTTP);
run with python3.12 -m unittest discover -s tests"""
import copy
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_partner_desk as pd  # noqa: E402  (installs the stubs, imports lambda_function)

lf = pd.lf
TODAY = date(2026, 11, 6)
LC, RF, SR, MSG = (lf.LAST_CONFIRMED_FIELD, lf.REFRESH_FIELD,
                   lf.SELLER_ROLE_FIELD, lf.MESSAGING_FIELD)


def deal(cf=None, stage=None, **kw):
    d = {"deal_stage": stage or {"id": 111800, "name": "Firm"}, "custom_fields": cf or {}}
    d.update(kw)
    return d


class RuleTests(unittest.TestCase):
    def st(self, *a, **k):
        return lf.unconfirmed_status(deal(*a, **k), today=TODAY)

    def test_blank_is_unconfirmed(self):
        self.assertEqual(self.st({}), (True, None))
        self.assertEqual(self.st({LC: ""}), (True, None))

    def test_default_60_days(self):
        self.assertFalse(self.st({LC: "2026/09/07"})[0])   # 60 days
        self.assertTrue(self.st({LC: "2026/09/06"})[0])    # 61 days

    def test_gp_alloc_30_days(self):
        self.assertFalse(self.st({LC: "2026/10/07", SR: 7020357})[0])
        self.assertTrue(self.st({LC: "2026/10/06", SR: [7020357]})[0])

    def test_refresh_overrides(self):
        self.assertTrue(self.st({LC: "2026/10/20", RF: "10", SR: 7020357})[0])
        self.assertFalse(self.st({LC: "2026/09/01", RF: 90.0})[0])
        self.assertEqual(self.st({RF: 0}), (False, None))
        self.assertEqual(self.st({RF: "0"}), (False, None))

    def test_hold_closed_archived_never(self):
        self.assertFalse(self.st({}, stage={"id": 2094373, "name": "Hold"})[0])
        self.assertFalse(self.st({}, stage={"id": 1, "name": "Won"})[0])
        self.assertFalse(self.st({}, stage={"id": 2348038, "name": "Obsolete"})[0])
        self.assertFalse(self.st({}, is_archived=True)[0])

    def test_fail_soft(self):
        self.assertEqual(self.st({LC: "not a date"}), (False, None))
        self.assertEqual(self.st({LC: "2026/09/01", RF: "abc"}), (False, None))
        self.assertEqual(lf.unconfirmed_status({"custom_fields": "junk"}, today=TODAY), (False, None))


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.saved = copy.deepcopy(pd.DEAL)
        pd.reset()

    def tearDown(self):
        pd.DEAL.clear()
        pd.DEAL.update(self.saved)

    def page(self, cf, type_id=5011675):
        pd.DEAL["custom_fields"] = dict({"custom_label_1958": type_id}, **cf)
        return pd.run()["body"]

    def test_confirmed_recently_no_notice_no_question(self):
        body = self.page({LC: lf.datetime.utcnow().strftime("%Y/%m/%d"), MSG: 7187010})
        self.assertNotIn('class="unconfirmed-notice"', body)
        self.assertNotIn("q_still_active", body)

    def test_old_date_allow_shows_notice_button_question(self):
        body = self.page({LC: "2025/03/04", MSG: 7187010})
        self.assertIn("Unconfirmed — last confirmed Mar 4, 2025. This order may no longer be available.", body)
        self.assertIn('title="&#9201; Unconfirmed — last confirmed Mar 4, 2025.', body)
        self.assertIn("Ask the seller to confirm", body)
        self.assertIn('name="q_still_active"', body)
        qa = body[body.index('class="qa-box"'):]
        self.assertLess(qa.index("q_still_active"), qa.index("Your email"))

    def test_blank_buy_order_says_buyer(self):
        body = self.page({MSG: 7187010}, type_id=5077819)
        self.assertIn("Unconfirmed — not recently confirmed. This order may no longer be available.", body)
        self.assertIn("Ask the buyer to confirm", body)

    def test_disallow_notice_only(self):
        body = self.page({LC: "2025/03/04", MSG: 7187011})
        self.assertIn('class="unconfirmed-notice"', body)
        self.assertNotIn("unconfirmed-ask", body.split("</style>", 1)[1])
        self.assertNotIn('class="qa-box"', body)

    def test_unparseable_no_notice(self):
        body = self.page({LC: "garbage", MSG: 7187010})
        self.assertNotIn('class="unconfirmed-notice"', body)
        self.assertNotIn("q_still_active", body)


if __name__ == "__main__":
    unittest.main()
