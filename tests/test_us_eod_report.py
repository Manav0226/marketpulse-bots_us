import datetime as dt
import unittest


class USEODReportTests(unittest.TestCase):
    def test_eod_report_not_due_before_close(self):
        from bot_us_eod_report import should_run_eod_report

        now = dt.datetime(2026, 5, 4, 19, 30, tzinfo=dt.timezone.utc)
        self.assertFalse(should_run_eod_report(now, None))

    def test_eod_report_due_after_close_once_per_day(self):
        from bot_us_eod_report import should_run_eod_report

        now = dt.datetime(2026, 5, 4, 20, 15, tzinfo=dt.timezone.utc)
        self.assertTrue(should_run_eod_report(now, None))
        self.assertFalse(should_run_eod_report(now, "2026-05-04T20:12:00+00:00"))


if __name__ == "__main__":
    unittest.main()
