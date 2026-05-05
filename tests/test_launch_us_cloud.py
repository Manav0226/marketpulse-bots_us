import unittest
import datetime as dt

from launcher import launch_us_cloud


class LaunchUSCloudTests(unittest.TestCase):
    def test_bots_include_shared_us_stack(self):
        self.assertEqual(
            set(launch_us_cloud.BOTS.keys()),
            {"father", "us", "us_intel", "us_scheduler"},
        )

    def test_restart_policy_blocks_clean_exit(self):
        self.assertFalse(launch_us_cloud.should_restart(0, 0))
        self.assertTrue(launch_us_cloud.should_restart(1, 0))

    def test_weekly_brief_stale_when_missing(self):
        self.assertTrue(launch_us_cloud._weekly_brief_stale(now=dt.datetime(2026, 5, 5, 12, 0, tzinfo=dt.timezone.utc)))


if __name__ == "__main__":
    unittest.main()
