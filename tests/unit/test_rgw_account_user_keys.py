# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""keys_match: whether the account-root user holds the vaulted key pair.

S3 rejects a key the cluster does not hold
the same way it rejects a user that does not exist.
rgw_tenants stops on keys_match false before any S3 step.

Run:  python3 -m unittest discover -s tests/unit
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json
import unittest
from unittest import mock

from test_rgw_oidc_replace import _bind_fqcn

_bind_fqcn()

from ansible.module_utils import basic  # noqa: E402
from module_args import patch_module_args  # noqa: E402

import rgw_account_user  # noqa: E402

VAULT = ("AKVAULT", "SKVAULT")
STALE = ("AKSTALE", "SKSTALE")


class Exited(Exception):

    def __init__(self, failed, kwargs):
        super(Exited, self).__init__(kwargs.get("msg", ""))
        self.failed = failed
        self.kwargs = kwargs


class FakeRGW(object):
    """radosgw-admin over one in-memory user. users maps uid -> list of (ak, sk)."""

    users = {}
    calls = []

    def __init__(self, module):
        self.exec_mode_used = "native"

    def run(self, *args, **kwargs):
        FakeRGW.calls.append(args)
        verb = args[:2]
        if verb == ("account", "list"):
            return 0, ["RGW00000000000000001"], "", ""
        if verb == ("user", "list"):
            return 0, sorted(FakeRGW.users), "", ""
        uid = args[args.index("--uid") + 1]
        if verb == ("user", "info"):
            if uid not in FakeRGW.users:
                return 2, None, "", "no such user"
            keys = [{"access_key": ak, "secret_key": sk} for ak, sk in FakeRGW.users[uid]]
            return 0, {"user_id": uid, "suspended": 0, "keys": keys}, "", ""
        ak = args[args.index("--access-key") + 1] if "--access-key" in args else None
        if verb == ("user", "create"):
            sk = args[args.index("--secret-key") + 1]
            FakeRGW.users[uid] = [(ak, sk)]
        elif verb == ("key", "create"):
            sk = args[args.index("--secret-key") + 1]
            FakeRGW.users[uid].append((ak, sk))
        elif verb == ("key", "rm"):
            FakeRGW.users[uid] = [k for k in FakeRGW.users[uid] if k[0] != ak]
        return 0, None, "", ""


def run_module(check_mode=False, **params):
    args = dict(uid="aviatoroot", account_id="RGW00000000000000001",
                access_key=VAULT[0], secret_key=VAULT[1], _ansible_check_mode=check_mode)
    args.update(params)

    def leave(failed):
        def _exit(self, **kwargs):
            raise Exited(failed, kwargs)
        return _exit

    with patch_module_args(args), \
            mock.patch.object(rgw_account_user, "RGW", FakeRGW), \
            mock.patch.object(basic.AnsibleModule, "exit_json", leave(False)), \
            mock.patch.object(basic.AnsibleModule, "fail_json", leave(True)):
        try:
            rgw_account_user.main()
        except Exited as exc:
            return exc
    raise AssertionError("module returned without exit_json")


class KeysMatch(unittest.TestCase):

    def setUp(self):
        FakeRGW.users = {}
        FakeRGW.calls = []

    def test_matching_pair_reports_true(self):
        FakeRGW.users["aviatoroot"] = [VAULT]
        res = run_module()
        self.assertTrue(res.kwargs["keys_match"])
        self.assertFalse(res.kwargs["changed"])

    def test_mismatch_is_reported_with_rotation_off_and_nothing_changes(self):
        FakeRGW.users["aviatoroot"] = [STALE]
        res = run_module()
        self.assertIs(res.kwargs["keys_match"], False)
        self.assertFalse(res.kwargs["changed"])
        self.assertEqual(FakeRGW.users["aviatoroot"], [STALE])

    def test_access_key_alone_is_not_a_match(self):
        FakeRGW.users["aviatoroot"] = [(VAULT[0], STALE[1])]
        self.assertIs(run_module().kwargs["keys_match"], False)

    def test_rotation_converges_and_reports_true(self):
        FakeRGW.users["aviatoroot"] = [STALE]
        res = run_module(rotate_keys=True)
        self.assertTrue(res.kwargs["keys_match"])
        self.assertEqual(FakeRGW.users["aviatoroot"], [VAULT])

    def test_check_mode_reports_a_pending_rotation_as_not_yet_matching(self):
        FakeRGW.users["aviatoroot"] = [STALE]
        res = run_module(check_mode=True, rotate_keys=True)
        self.assertIs(res.kwargs["keys_match"], False)
        self.assertIn("rotated", res.kwargs["actions"])
        self.assertEqual(FakeRGW.users["aviatoroot"], [STALE])

    def test_check_mode_mismatch_without_rotation_is_false(self):
        FakeRGW.users["aviatoroot"] = [STALE]
        self.assertIs(run_module(check_mode=True).kwargs["keys_match"], False)

    def test_missing_user_in_check_mode_is_null_and_not_created(self):
        res = run_module(check_mode=True)
        self.assertIsNone(res.kwargs["keys_match"])
        self.assertNotIn("aviatoroot", FakeRGW.users)

    def test_created_user_matches(self):
        self.assertTrue(run_module().kwargs["keys_match"])

    def test_no_keys_given_is_null(self):
        FakeRGW.users["aviatoroot"] = [STALE]
        res = run_module(access_key=None, secret_key=None)
        self.assertIsNone(res.kwargs["keys_match"])


if __name__ == "__main__":
    unittest.main()
