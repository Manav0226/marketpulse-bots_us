import unittest

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


if __name__ == "__main__":
    unittest.main()
