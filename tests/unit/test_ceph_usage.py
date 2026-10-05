# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""Usage-vs-quota arithmetic of the ceph_usage filters.

Run:  python3 -m unittest discover -s tests/unit
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                "..", "..", "plugins", "filter"))

from ceph_usage import ceph_over_quota, ceph_usage_report  # noqa: E402


def account(aid="RGW1", name="tenant-acme", quota=None, stats=None):
    return {"id": aid, "name": name, "quota": quota or {}, "stats": stats or {}}


class TestPercentages(unittest.TestCase):

    def test_a_finite_cap_gives_a_percentage(self):
        row = ceph_usage_report([account(
            quota={"max_size": 1000, "max_objects": 200},
            stats={"size": 250, "num_objects": 10})])[0]
        self.assertEqual(row["size_pct"], 25.0)
        self.assertEqual(row["objects_pct"], 5.0)

    def test_unlimited_is_none_not_zero(self):
        """-1 is RGW's unlimited. Dividing by it would report a negative percentage."""
        row = ceph_usage_report([account(
            quota={"max_size": -1, "max_objects": -1},
            stats={"size": 250, "num_objects": 10})])[0]
        self.assertIsNone(row["size_pct"])
        self.assertIsNone(row["objects_pct"])

    def test_a_zero_cap_is_unlimited_too_rather_than_a_division_by_zero(self):
        row = ceph_usage_report([account(quota={"max_size": 0},
                                         stats={"size": 250})])[0]
        self.assertIsNone(row["size_pct"])

    def test_a_missing_quota_reports_unlimited_and_zero_usage(self):
        row = ceph_usage_report([account()])[0]
        self.assertEqual((row["used_size"], row["max_size"], row["size_pct"]),
                         (0, -1, None))
        self.assertFalse(row["enabled"])


class TestNaming(unittest.TestCase):

    def test_the_configured_tenant_name_wins_over_the_account_name(self):
        rows = ceph_usage_report([account(name="tenant-acme")],
                                 [{"account_id": "RGW1", "name": "acme"}])
        self.assertEqual(rows[0]["name"], "acme")

    def test_an_unmanaged_account_keeps_its_own_name(self):
        rows = ceph_usage_report([account(aid="RGW9", name="tenant-stray")], [])
        self.assertEqual(rows[0]["name"], "tenant-stray")

    def test_a_nameless_unmanaged_account_falls_back_to_its_id(self):
        rows = ceph_usage_report([account(aid="RGW9", name="")], [])
        self.assertEqual(rows[0]["name"], "RGW9")


class TestOverQuota(unittest.TestCase):

    def test_the_threshold_is_inclusive_on_either_cap(self):
        rows = ceph_usage_report([
            account(aid="RGW1", name="at", quota={"max_size": 100},
                    stats={"size": 80}),
            account(aid="RGW2", name="under", quota={"max_size": 100},
                    stats={"size": 79}),
            account(aid="RGW3", name="objects", quota={"max_objects": 10},
                    stats={"num_objects": 9}),
        ])
        self.assertEqual(ceph_over_quota(rows), ["at", "objects"])

    def test_unlimited_accounts_never_appear(self):
        rows = ceph_usage_report([account(quota={"max_size": -1},
                                          stats={"size": 10 ** 12})])
        self.assertEqual(ceph_over_quota(rows), [])

    def test_a_tenant_over_both_caps_is_listed_once(self):
        rows = ceph_usage_report([account(
            name="both", quota={"max_size": 10, "max_objects": 10},
            stats={"size": 10, "num_objects": 10})])
        self.assertEqual(ceph_over_quota(rows), ["both"])


if __name__ == "__main__":
    unittest.main()
