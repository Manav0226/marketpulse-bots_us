import datetime as dt
import unittest


class VMUSSchedulerTests(unittest.TestCase):
    def test_weekly_job_due_on_matching_weekday_and_time(self):
        from vm_us_scheduler import is_job_due

        job = {"kind": "weekly", "weekday": 6, "time_utc": "13:00"}
        now = dt.datetime(2026, 5, 10, 13, 0, tzinfo=dt.timezone.utc)

        self.assertTrue(is_job_due(job, now, last_run=None))

    def test_weekly_job_not_due_twice_same_day(self):
        from vm_us_scheduler import is_job_due

        job = {"kind": "weekly", "weekday": 6, "time_utc": "13:00"}
        now = dt.datetime(2026, 5, 10, 13, 5, tzinfo=dt.timezone.utc)
        last_run = "2026-05-10T13:00:00+00:00"

        self.assertFalse(is_job_due(job, now, last_run=last_run))

    def test_interval_job_due_after_elapsed_minutes(self):
        from vm_us_scheduler import is_job_due

        job = {"kind": "interval", "every_minutes": 10}
        now = dt.datetime(2026, 5, 10, 13, 20, tzinfo=dt.timezone.utc)
        last_run = "2026-05-10T13:09:00+00:00"

        self.assertTrue(is_job_due(job, now, last_run=last_run))

    def test_build_schedule_contains_research_and_supervision_jobs(self):
        from vm_us_scheduler import build_us_vm_jobs

        jobs = build_us_vm_jobs()
        names = {job["name"] for job in jobs}

        self.assertIn("us_research_weekly", names)
        self.assertIn("us_research_daily", names)
        self.assertIn("us_supervision_refresh", names)


if __name__ == "__main__":
    unittest.main()
