"""Unconfirmed notice tests. Reuses the Partner Desk stubs (S3 + HTTP);
run with python3.12 -m unittest discover -s tests"""
import copy
import os
from datetime import datetime, timedelta, timezone
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


LOG = ("full-pipeline-cache", "obsolete-review/log.json")


def reset_log(entries=None):
    """Install (or remove, if None) the fake restore log and clear the 10-min cache."""
    pd.FakeS3.objects.pop(LOG, None)
    if entries is not None:
        pd.FakeS3.objects[LOG] = {"entries": entries}
    lf._restore_log_cache.update(by_deal=None, loaded_at=0.0)


def pipe_ts(d):
    return d.strftime("%Y/%m/%d %H:%M:%S +0000")


def ago(days):
    return datetime(TODAY.year, TODAY.month, TODAY.day, 12, tzinfo=timezone.utc) - timedelta(days=days)


class RuleTests(unittest.TestCase):
    def setUp(self):
        reset_log()

    def tearDown(self):
        reset_log()

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


class UpdatedAtTests(unittest.TestCase):
    """Effective date = newer of Last Confirmed and updated_at, with restore exception."""
    def setUp(self):
        reset_log()

    def tearDown(self):
        reset_log()

    def st(self, cf=None, updated=None, stage=None):
        kw = {"id": 4242}
        if updated is not None:
            kw["updated_at"] = pipe_ts(updated)
        return lf.unconfirmed_status(deal(cf, stage=stage, **kw), today=TODAY)

    def restore_at(self, action="restored", days=150):
        t = ago(days)
        reset_log([
            {"deal_id": 4242, "action": "kept", "at": (t - timedelta(days=5)).isoformat()},
            {"deal_id": 777, "action": "restored", "at": t.isoformat()},
            {"deal_id": 4242, "action": action, "at": t.strftime("%Y-%m-%dT%H:%M:%SZ")},
        ])
        return t

    def test_blank_lc_recent_edit_confirmed(self):
        self.assertEqual(self.st({}, ago(1)), (False, ago(1).date()))

    def test_blank_lc_old_edit_unconfirmed_with_date(self):
        self.assertEqual(self.st({}, ago(100)), (True, ago(100).date()))

    def test_recent_lc_old_edit(self):
        lc = ago(10).date()
        self.assertEqual(self.st({LC: lc.strftime("%Y-%m-%d")}, ago(100)), (False, lc))

    def test_updated_at_converted_to_utc_date(self):
        d = deal({}, id=1, updated_at="2026/09/07 22:30:00 -0400")   # = 2026/09/08 UTC
        self.assertEqual(lf.unconfirmed_status(d, today=TODAY), (False, date(2026, 9, 8)))

    def test_restore_write_does_not_confirm(self):
        t = self.restore_at()
        lc = ago(200).date()
        self.assertEqual(self.st({LC: lc.strftime("%Y/%m/%d")}, t + timedelta(minutes=1)), (True, lc))

    def test_restore_then_blank_lc_is_not_recently_confirmed(self):
        t = self.restore_at()
        self.assertEqual(self.st({}, t + timedelta(minutes=15)), (True, None))

    def test_edit_after_restore_counts(self):
        t = self.restore_at(days=4)
        lc = ago(200).date()
        self.assertEqual(self.st({LC: lc.strftime("%Y/%m/%d")}, t + timedelta(days=2)), (False, ago(2).date()))

    def test_latest_entry_undone_plain_rule(self):
        t = self.restore_at(action="undone")
        self.assertEqual(self.st({LC: ago(200).strftime("%Y/%m/%d")}, t + timedelta(minutes=1)),
                         (True, (t + timedelta(minutes=1)).date()))
        self.assertEqual(self.st({}, ago(1)), (False, ago(1).date()))

    def test_other_deal_restore_ignored(self):
        reset_log([{"deal_id": 777, "action": "restored", "at": ago(1).isoformat()}])
        self.assertEqual(self.st({}, ago(1)), (False, ago(1).date()))

    def test_log_unreadable_plain_rule(self):
        orig = pd.FakeS3.get_object

        def denied(self, Bucket, Key):
            if Key == LOG[1]:
                raise RuntimeError("AccessDenied")
            return orig(self, Bucket, Key)
        pd.FakeS3.get_object = denied
        try:
            self.assertEqual(self.st({}, ago(1)), (False, ago(1).date()))
        finally:
            pd.FakeS3.get_object = orig

    def test_log_malformed_plain_rule(self):
        pd.FakeS3.objects[LOG] = ["not", "a", "dict"]
        lf._restore_log_cache.update(by_deal=None, loaded_at=0.0)
        self.assertEqual(self.st({}, ago(1)), (False, ago(1).date()))
        self.assertEqual(lf._restore_log_latest(), {})

    def test_log_cached_ten_minutes(self):
        self.restore_at()
        lf._restore_log_latest()
        n = pd.FakeS3.gets.count(LOG[1])
        lf._restore_log_latest()
        self.assertEqual(pd.FakeS3.gets.count(LOG[1]), n)
        lf._restore_log_cache["loaded_at"] -= 601
        lf._restore_log_latest()
        self.assertEqual(pd.FakeS3.gets.count(LOG[1]), n + 1)

    def test_hold_and_refresh_zero_never(self):
        self.assertEqual(self.st({}, ago(400), stage={"id": 2094373, "name": "Hold"}), (False, None))
        self.assertEqual(self.st({RF: 0}, ago(400)), (False, None))


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.saved = copy.deepcopy(pd.DEAL)
        pd.reset()
        reset_log()

    def tearDown(self):
        pd.DEAL.clear()
        pd.DEAL.update(self.saved)
        reset_log()

    def page(self, cf, type_id=5011675, updated="2024/01/02 10:00:00 +0000"):
        # updated_at now counts as confirmation; keep it old (or absent) so these
        # tests exercise the Last Confirmed path.
        pd.DEAL["custom_fields"] = dict({"custom_label_1958": type_id}, **cf)
        if updated is None:
            pd.DEAL.pop("updated_at", None)
        else:
            pd.DEAL["updated_at"] = updated
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
        body = self.page({MSG: 7187010}, type_id=5077819, updated=None)
        self.assertIn("Unconfirmed — not recently confirmed. This order may no longer be available.", body)
        self.assertIn("Ask the buyer to confirm", body)

    def test_blank_lc_recent_edit_no_notice(self):
        body = self.page({MSG: 7187010}, updated=lf.datetime.utcnow().strftime("%Y/%m/%d %H:%M:%S +0000"))
        self.assertNotIn('class="unconfirmed-notice"', body)
        self.assertNotIn("q_still_active", body)

    def test_blank_lc_old_edit_shows_edit_date(self):
        body = self.page({MSG: 7187010}, updated="2025/06/15 10:00:00 +0000")
        self.assertIn("Unconfirmed — last confirmed Jun 15, 2025.", body)

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
