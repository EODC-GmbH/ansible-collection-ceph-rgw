# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""A failed radosgw-admin read is absent only when a listing confirms it.

An unreachable cluster must not read as a missing user.

Run:  python3 -m unittest discover -s tests/unit
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import unittest

from test_rgw_oidc_replace import _bind_fqcn

_bind_fqcn()

from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw import RGWError  # noqa: E402
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils import rgw_read  # noqa: E402

ACCOUNT = "RGW00000000000000001"


class FakeRGW(object):
    """Answers from a table keyed by the first two words; check_rc raises like RGW.run."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def run(self, *args, **kwargs):
        self.calls.append(args)
        rc, parsed, out = self.answers[args[:2]]
        if callable(rc):
            rc = rc()
        if rc != 0 and kwargs.get("check_rc", True):
            raise RGWError("radosgw-admin said no", rc=rc)
        return rc, parsed, out, ""


def flaky(*codes):
    """An exit code per call, so a transient failure can recover on the retry."""
    seq = list(codes)
    return lambda: seq.pop(0)


class ReadUser(unittest.TestCase):

    def test_a_present_user_is_returned(self):
        fake = FakeRGW({("user", "info"): (0, {"user_id": "aviatoroot"}, "{}")})
        self.assertEqual(rgw_read.read_user(fake, "aviatoroot", ACCOUNT)["user_id"], "aviatoroot")

    def test_absent_when_the_account_lists_no_such_user(self):
        fake = FakeRGW({("user", "info"): (22, None, ""),
                        ("account", "list"): (0, [ACCOUNT], "[]"),
                        ("user", "list"): (0, ["other"], "[]")})
        self.assertIsNone(rgw_read.read_user(fake, "aviatoroot", ACCOUNT))

    def test_absent_when_the_account_itself_is_gone(self):
        fake = FakeRGW({("user", "info"): (22, None, ""),
                        ("account", "list"): (0, [], "[]")})
        self.assertIsNone(rgw_read.read_user(fake, "aviatoroot", ACCOUNT))

    def test_an_unreachable_cluster_raises_instead_of_reading_absent(self):
        fake = FakeRGW({("user", "info"): (110, None, ""),
                        ("account", "list"): (110, None, "")})
        with self.assertRaises(RGWError):
            rgw_read.read_user(fake, "aviatoroot", ACCOUNT)

    def test_a_listed_user_whose_info_fails_raises(self):
        fake = FakeRGW({("user", "info"): (5, None, ""),
                        ("account", "list"): (0, [ACCOUNT], "[]"),
                        ("user", "list"): (0, ["aviatoroot"], "[]")})
        with self.assertRaises(RGWError):
            rgw_read.read_user(fake, "aviatoroot", ACCOUNT)

    def test_a_transient_failure_recovers_on_the_retry(self):
        fake = FakeRGW({("user", "info"): (flaky(5, 0), {"user_id": "aviatoroot"}, "{}"),
                        ("account", "list"): (0, [ACCOUNT], "[]"),
                        ("user", "list"): (0, ["aviatoroot"], "[]")})
        self.assertEqual(rgw_read.read_user(fake, "aviatoroot", ACCOUNT)["user_id"], "aviatoroot")


class Unparseable(unittest.TestCase):

    def test_success_without_json_raises_and_does_not_echo_the_output(self):
        """`user info` prints secret keys, so they must not reach the error."""
        fake = FakeRGW({("user", "info"): (0, None, "banner\n{\"secret_key\": \"S\"}")})
        with self.assertRaises(RGWError) as caught:
            rgw_read.read_user(fake, "aviatoroot", ACCOUNT)
        self.assertNotIn("secret", caught.exception.msg)

    def test_a_listing_that_is_not_an_array_raises(self):
        fake = FakeRGW({("account", "get"): (2, None, ""),
                        ("account", "list"): (0, None, "not json")})
        with self.assertRaises(RGWError):
            rgw_read.read_account(fake, ACCOUNT)


class ReadAccountAndRoles(unittest.TestCase):

    def test_a_missing_account_is_absent(self):
        fake = FakeRGW({("account", "get"): (2, None, ""),
                        ("account", "list"): (0, [], "[]")})
        self.assertIsNone(rgw_read.read_account(fake, ACCOUNT))

    def test_role_list_failing_on_a_live_account_raises(self):
        fake = FakeRGW({("role", "list"): (5, None, ""),
                        ("account", "list"): (0, [ACCOUNT], "[]")})
        with self.assertRaises(RGWError):
            rgw_read.read_roles(fake, ACCOUNT)

    def test_no_output_from_role_list_is_an_empty_list(self):
        fake = FakeRGW({("role", "list"): (0, None, "")})
        self.assertEqual(rgw_read.read_roles(fake, ACCOUNT), [])

    def test_no_output_from_user_list_means_no_users(self):
        """An account whose last user was removed lists nothing."""
        fake = FakeRGW({("user", "info"): (22, None, ""),
                        ("account", "list"): (0, [ACCOUNT], "[]"),
                        ("user", "list"): (0, None, "")})
        self.assertIsNone(rgw_read.read_user(fake, "aviatoroot", ACCOUNT))

    def test_an_empty_role_list_is_an_empty_list(self):
        fake = FakeRGW({("role", "list"): (0, [], "[]")})
        self.assertEqual(rgw_read.read_roles(fake, ACCOUNT), [])


if __name__ == "__main__":
    unittest.main()
