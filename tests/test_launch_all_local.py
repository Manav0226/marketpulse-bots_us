import unittest


class LaunchAllLocalTests(unittest.TestCase):
    def test_local_launcher_keeps_father_disabled_by_default(self):
        from launcher import launch_all

        self.assertIn("father", launch_all.BOTS)
        self.assertFalse(launch_all.BOTS["father"]["enabled"])


if __name__ == "__main__":
    unittest.main()
