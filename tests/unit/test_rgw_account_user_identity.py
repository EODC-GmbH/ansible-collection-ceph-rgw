# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""Identity drift on an existing user: display name and account-root status.

Both are set only when the user is created.
The display name is part of the user ARN that trust policies
and IAM UserName lookups match on.
The module stops and names the difference, under --check too, and repairs neither.
Read-only probes pass check_identity false.

Run:  python3 -m unittest discover -s tests/unit
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import unittest

from fake_aws import ACCOUNT, FakeAWS, run_module
from fake_cluster import Cluster

import rgw_account_user  # noqa: E402

VAULT = ("AKVAULT", "SKVAULT")


def params(**kw):
    return dict(uid="aviatoroot", account_id=ACCOUNT, access_key=VAULT[0],
                secret_key=VAULT[1], **kw)


class Identity(unittest.TestCase):

    def setUp(self):
        self.cluster = Cluster()
        self.cluster.add_account(ACCOUNT)

    def run_user(self, check_mode=False, **kw):
        return run_module(rgw_account_user, params(**kw), self.cluster, FakeAWS(self.cluster),
                          check_mode=check_mode)

    def assert_stopped(self, needle, **kw):
        for check_mode in (True, False):
            res = self.run_user(check_mode=check_mode, **kw)
            self.assertTrue(res.failed, "check_mode=%s did not fail" % check_mode)
            self.assertIn(needle, res.kwargs["msg"])
            self.assertEqual(self.cluster.writes, [])

    def test_display_name_drift_stops_the_run(self):
        self.cluster.add_user(ACCOUNT, "aviatoroot", [VAULT], display_name="Aviato Root")
        self.assert_stopped("display name is 'Aviato Root'")

    def test_explicit_display_name_is_the_one_compared(self):
        self.cluster.add_user(ACCOUNT, "aviatoroot", [VAULT], display_name="Aviato Root")
        self.assertFalse(self.run_user(display_name="Aviato Root").failed)

    def test_plain_user_declared_root_stops_the_run(self):
        self.cluster.add_user(ACCOUNT, "aviatoroot", [VAULT], account_root=False)
        self.assert_stopped("is not an account-root user")

    def test_root_user_declared_plain_stops_the_run(self):
        self.cluster.add_user(ACCOUNT, "aviatoroot", [VAULT])
        self.assert_stopped("is an account-root user", account_root=False)

    def test_stops_before_a_pending_rotation(self):
        self.cluster.add_user(ACCOUNT, "aviatoroot", [("AKOLD", "SKOLD")], account_root=False)
        self.assert_stopped("is not an account-root user", rotate_keys=True)

    def test_a_build_without_a_user_type_is_not_compared(self):
        self.cluster.add_user(ACCOUNT, "aviatoroot", [VAULT])
        del self.cluster.users[(ACCOUNT, "aviatoroot")]["type"]
        res = self.run_user(account_root=False)
        self.assertFalse(res.failed)
        self.assertNotIn("account_root", res.kwargs["user"])

    def test_probe_with_check_identity_off_still_reports_keys(self):
        self.cluster.add_user(ACCOUNT, "aviatoroot", [VAULT], display_name="Aviato Root",
                              account_root=False)
        res = self.run_user(check_mode=True, check_identity=False)
        self.assertFalse(res.failed)
        self.assertTrue(res.kwargs["keys_match"])

    def test_absent_removes_a_drifted_user(self):
        self.cluster.add_user(ACCOUNT, "aviatoroot", [VAULT], display_name="Aviato Root")
        res = self.run_user(state="absent")
        self.assertFalse(res.failed)
        self.assertNotIn((ACCOUNT, "aviatoroot"), self.cluster.users)

    def test_matching_user_reports_both_fields(self):
        self.cluster.add_user(ACCOUNT, "aviatoroot", [VAULT])
        res = self.run_user()
        self.assertFalse(res.failed)
        self.assertFalse(res.kwargs["changed"])
        self.assertEqual(res.kwargs["user"]["display_name"], "aviatoroot")
        self.assertTrue(res.kwargs["user"]["account_root"])

    def test_created_user_matches_on_the_next_run(self):
        self.assertTrue(self.run_user(account_root=False).kwargs["changed"])
        res = self.run_user(account_root=False)
        self.assertFalse(res.failed, res.kwargs.get("msg"))
        self.assertFalse(res.kwargs["user"]["account_root"])


if __name__ == "__main__":
    unittest.main()
