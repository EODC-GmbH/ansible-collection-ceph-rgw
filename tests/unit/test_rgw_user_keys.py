# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""Per-key state of an account user, and the check-mode diff of key and suspend changes.

Run:  python3 -m unittest discover -s tests/unit
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import importlib
import unittest

from test_rgw_oidc_replace import _bind_fqcn

_bind_fqcn()

from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_user import (  # noqa: E402
    key_conflicts, plan_keys, project,
)
from ceph_releases import RELEASES  # noqa: E402
from converge_contract import ROOT, VAULT, account, fresh, root, seeded  # noqa: E402
from fake_aws import ACCOUNT, run_module  # noqa: E402
from test_idempotency import oidc, user  # noqa: E402

PRIMARY = ("AKP", "SKP")


def held(*pairs):
    return {"keys": [{"access_key": a, "secret_key": s} for a, s in pairs]}


def present(access_key, secret_key):
    return {"access_key": access_key, "secret_key": secret_key, "state": "present"}


def absent(access_key):
    return {"access_key": access_key, "secret_key": None, "state": "absent"}


class TestPlanKeys(unittest.TestCase):

    def test_plan(self):
        cases = [
            # name, held, declared keys, rotate, prune, (create, remove, drift)
            ("converged, extra key left alone",
             held(PRIMARY, ("AKX", "SKX")), [], False, True, ([], [], [])),
            ("absent key removed without rotate",
             held(PRIMARY, ("AKOLD", "SKOLD")), [absent("AKOLD")], False, True,
             ([], ["AKOLD"], [])),
            ("absent key already gone",
             held(PRIMARY), [absent("AKOLD")], False, True, ([], [], [])),
            ("present key added without rotate",
             held(PRIMARY), [present("AKB", "SKB")], False, True,
             ([("AKB", "SKB")], [], [])),
            ("present key secret drift reported, not fixed",
             held(PRIMARY, ("AKB", "SKOLD")), [present("AKB", "SKB")], False, True,
             ([], [], ["AKB"])),
            ("present key secret drift re-set with rotate",
             held(PRIMARY, ("AKB", "SKOLD")), [present("AKB", "SKB")], True, False,
             ([("AKB", "SKB")], [], ["AKB"])),
            ("rotation installs primary, prunes the rest",
             held(("AKOLD", "SKOLD")), [], True, True, ([PRIMARY], ["AKOLD"], [])),
            ("interrupted rotation: primary held, stale key pruned",
             held(PRIMARY, ("AKSTALE", "SKSTALE")), [], True, True,
             ([], ["AKSTALE"], [])),
            ("rotation prune keeps declared present keys",
             held(PRIMARY, ("AKB", "SKB"), ("AKSTALE", "SKSTALE")),
             [present("AKB", "SKB")], True, True, ([], ["AKSTALE"], [])),
            ("rotation without prune keeps undeclared keys",
             held(("AKOLD", "SKOLD")), [], True, False, ([PRIMARY], [], [])),
            ("new user: present key added",
             None, [present("AKB", "SKB")], False, True, ([("AKB", "SKB")], [], [])),
        ]
        for name, info, keys, rotate, prune, expected in cases:
            with self.subTest(name):
                self.assertEqual(plan_keys(info, PRIMARY, keys, rotate, prune), expected)

    def test_conflicts(self):
        cases = [
            ("clean", [present("AKB", "SKB"), absent("AKOLD")], 0),
            ("present and absent", [present("AKB", "SKB"), absent("AKB")], 1),
            ("primary absent", [absent(PRIMARY[0])], 1),
        ]
        for name, keys, count in cases:
            with self.subTest(name):
                self.assertEqual(len(key_conflicts(PRIMARY, keys)), count)

    def test_project(self):
        before = {"present": True, "suspended": True, "access_key_ids": ["AKOLD", "AKP"]}
        after = project(before, [("AKB", "SKB")], ["AKOLD"], False)
        self.assertEqual(after["access_key_ids"], ["AKB", "AKP"])
        self.assertFalse(after["suspended"])


def stale_key(cluster, aws):
    cluster.add_user(ACCOUNT, ROOT, [VAULT, ("AKSTALE", "SKSTALE")])


def suspended_root(cluster, aws):
    account(cluster, aws)
    cluster.add_user(ACCOUNT, ROOT, [VAULT], suspended=1)


class TestModule(unittest.TestCase):

    def run_user(self, seed, check_mode, **params):
        results = []
        for release in RELEASES:
            cluster, aws = fresh(seed, release)
            res = run_module(importlib.import_module("rgw_account_user"), params,
                             cluster, aws, check_mode=check_mode)
            results.append((release, res, cluster))
        return results

    def test_check_diff_shows_the_absent_key_going(self):
        for release, res, cluster in self.run_user(
                seeded(account, stale_key), True, keys=[absent("AKSTALE")], **user()):
            diff = res.kwargs["diff"]
            self.assertIn("AKSTALE", diff["before"]["access_key_ids"], release)
            self.assertNotIn("AKSTALE", diff["after"]["access_key_ids"], release)
            self.assertEqual(res.kwargs["actions"], ["keys_removed"], release)
            self.assertEqual(cluster.writes, [], release)

    def test_check_diff_shows_the_enable(self):
        for release, res, _ in self.run_user(suspended_root, True, **user()):
            diff = res.kwargs["diff"]
            self.assertTrue(diff["before"]["suspended"], release)
            self.assertFalse(diff["after"]["suspended"], release)

    def test_undeclared_key_is_reported_not_removed(self):
        for release, res, cluster in self.run_user(seeded(account, stale_key), False,
                                                   **user()):
            self.assertFalse(res.kwargs["changed"], release)
            self.assertEqual(res.kwargs["unmanaged_key_ids"], ["AKSTALE"], release)

    def test_conflicting_declaration_changes_nothing(self):
        for release, res, cluster in self.run_user(
                seeded(account, stale_key), False, keys=[absent(VAULT[0])], **user()):
            self.assertTrue(res.failed, release)
            self.assertEqual(cluster.writes, [], release)

    def test_suspended_holder_still_fails_a_real_run(self):
        for release in RELEASES:
            cluster, aws = fresh(suspended_root, release)
            res = run_module(importlib.import_module("rgw_oidc_provider"), oidc(),
                             cluster, aws)
            self.assertTrue(res.failed, release)
            self.assertIn("suspended", res.kwargs["msg"], release)


if __name__ == "__main__":
    unittest.main()
