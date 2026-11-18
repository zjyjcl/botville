"""init.sh: the three pairing tokens must be pairwise different (A-R4, #8).

Reuses the temp-tree harness of test_push_sygnal.SygnalInitScriptTest (a
copy of server/scripts + a minimal server/compose) without inheriting its
tests. Token values here are obviously fake fixtures; init.sh must never
print them.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_push_sygnal as base  # noqa: E402

H = base.SygnalInitScriptTest
FAKE = "fixture-pairing-token-" + "0" * 24


class InitPairingTokensTest(unittest.TestCase):
    setUp = H.setUp
    run_init = H.run_init
    write_env = H.write_env

    def tearDown(self):
        if hasattr(H, "tearDown"):
            H.tearDown(self)

    def env(self, admin, service, send):
        return (base.APNS_DISABLED_ENV
                + "PAIRING_ADMIN_TOKEN={}\n".format(admin)
                + "PAIRING_SERVICE_TOKEN={}\n".format(service)
                + "PAIRING_RELATIONS_SEND_TOKEN={}\n".format(send))

    def test_distinct_tokens_pass(self):
        self.write_env(self.env(FAKE + "a", FAKE + "b", FAKE + "c"))
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_equal_pairs_are_refused_without_printing_values(self):
        cases = {
            ("PAIRING_ADMIN_TOKEN", "PAIRING_SERVICE_TOKEN"):
                (FAKE, FAKE, FAKE + "c"),
            ("PAIRING_ADMIN_TOKEN", "PAIRING_RELATIONS_SEND_TOKEN"):
                (FAKE, FAKE + "b", FAKE),
            ("PAIRING_SERVICE_TOKEN", "PAIRING_RELATIONS_SEND_TOKEN"):
                (FAKE + "a", FAKE, FAKE),
        }
        for (k1, k2), values in cases.items():
            with self.subTest(pair=(k1, k2)):
                self.write_env(self.env(*values))
                proc = self.run_init()
                out = proc.stdout + proc.stderr
                self.assertNotEqual(proc.returncode, 0, out)
                self.assertIn("{} and {}".format(k1, k2), out)
                self.assertNotIn(FAKE, out)

    def test_empty_tokens_are_left_alone(self):
        self.write_env(self.env("", "", ""))
        proc = self.run_init()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
