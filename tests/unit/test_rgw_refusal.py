# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""A refused key: missing prerequisite or real failure, as radosgw-admin decides.

Squid answers AccessDenied (HeadBucket a bare 403) both for a key no user holds
and for a user that may not do the call. Each case runs on every release in RELEASES.
The first (a greenfield tenant in --check, or a torn-down one) passes
and the second fails.

Run:  python3 -m unittest discover -s tests/unit
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import importlib
import unittest

from ceph_releases import RELEASES
from fake_aws import ACCOUNT, FakeAWS, run_module
from fake_cluster import Cluster
from test_idempotency import POLICY, S3, VAULT, oidc

PRESENT = [
    ("rgw_bucket", dict(S3, names=["aviato-a"])),
    ("rgw_oidc_provider", oidc()),
    ("rgw_user_policy", dict(S3, user_name="svc", policy=POLICY)),
]
ABSENT = [
    ("rgw_bucket", dict(S3, names=["aviato-a"], state="absent")),
    ("rgw_oidc_provider", oidc(state="absent")),
]


def cluster_holding_the_key(release):
    cluster = Cluster(release)
    cluster.add_account(ACCOUNT, "tenant-aviato")
    cluster.add_user(ACCOUNT, "aviatoroot", [VAULT])
    return cluster


class Unreachable(Cluster):
    """A cluster radosgw-admin cannot reach: every command fails."""

    def handle(self, argv):
        return 1, "", "failed to fetch mon config (--no-mon-config to skip)"


class Refusal(unittest.TestCase):

    def runs(self, rows, cluster_factory, modes):
        """(label, Exited) for each row in each mode and release, against an IAM/S3 that
        refuses: the key is unknown to the cluster, or its holder lacks permission."""
        out = []
        for release in RELEASES:
            for check_mode in modes:
                for name, params in rows:
                    cluster = cluster_factory(release)
                    aws = FakeAWS(cluster, denied=True)
                    res = run_module(importlib.import_module(name), params, cluster,
                                     aws, check_mode=check_mode)
                    self.assertEqual(aws.writes, [], "%s wrote after a refusal" % name)
                    out.append(("%s %s state=%s check_mode=%s"
                                % (release, name, params.get("state", "present"),
                                   check_mode), res))
        return out

    def test_a_refused_key_that_a_user_holds_fails(self):
        for label, res in self.runs(PRESENT + ABSENT, cluster_holding_the_key, (True, False)):
            with self.subTest(label):
                self.assertTrue(res.failed, res.kwargs)
                self.assertIn("aviatoroot", res.kwargs["msg"])

    def test_a_key_nobody_holds_fails_a_real_present_run(self):
        """rgw_tenants creates the root user first, so outside --check this is an error."""
        for label, res in self.runs(PRESENT, Cluster, (False,)):
            with self.subTest(label):
                self.assertTrue(res.failed, res.kwargs)
                self.assertIn("no RGW user holds", res.kwargs["msg"])

    def test_teardown_after_the_account_is_gone_passes_unchanged(self):
        """Teardown removes the root user, so a rerun signs with keys nobody holds."""
        for label, res in self.runs(ABSENT, Cluster, (True, False)):
            with self.subTest(label):
                self.assertFalse(res.failed, res.kwargs.get("msg"))
                self.assertFalse(res.kwargs["changed"])
                self.assertFalse(res.kwargs["probed"])

    def test_an_unreachable_cluster_fails_instead_of_guessing(self):
        for label, res in self.runs(PRESENT + ABSENT, Unreachable, (True,)):
            with self.subTest(label):
                self.assertTrue(res.failed, res.kwargs)
                self.assertIn("could not tell", res.kwargs["msg"])


if __name__ == "__main__":
    unittest.main()
