# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""Module steps in rgw_tenants' task order, held to the converge-twice contract.

A single-module row cannot show one step's --check depending on an earlier step that --check
did not carry out, which is what a preview of a whole role meets. These steps follow
roles/rgw_tenants/tasks/provision*.yml and teardown.yml; tests/integration/inmemory.sh runs the
roles themselves against the same fakes.

Run:  python3 -m unittest discover -s tests/unit
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import importlib
import json
import unittest

from ceph_releases import RELEASES
from converge_contract import ROOT, ConvergeCase, fresh, no_seed, root, seeded
from fake_aws import ACCOUNT, run_module
from test_idempotency import POLICY, S3, oidc, user, with_provider

SVC = "iam-aviato-svc-etl"
SVC_KEYS = ("AKSVC", "SKSVC")

PROVISION = [
    ("rgw_account", dict(account_id=ACCOUNT, name="tenant-aviato")),
    ("rgw_account_user", user()),
    ("rgw_account_user", dict(uid=SVC, account_id=ACCOUNT, account_root=False,
                              access_key=SVC_KEYS[0], secret_key=SVC_KEYS[1])),
    ("rgw_oidc_provider", oidc()),
    ("rgw_bucket", dict(S3, names=["aviato-a", "aviato-b"])),
    ("rgw_user_policy", dict(S3, user_name=SVC, policy=POLICY)),
]

# teardown.yml first compares the vaulted keys in forced check mode; that probe changes
# nothing, so it is left out.
TEARDOWN = [
    ("rgw_bucket", dict(S3, names=["aviato-a", "aviato-b"], state="absent")),
    ("rgw_oidc_provider", oidc(state="absent")),
    ("rgw_account_user", dict(uid=SVC, account_id=ACCOUNT, state="absent")),
    ("rgw_account_user", dict(user(), state="absent")),
    ("rgw_account", dict(account_id=ACCOUNT, state="absent")),
]


def provisioned(cluster, aws):
    """The state PROVISION converges to, written directly."""
    root(cluster, aws)
    cluster.add_user(ACCOUNT, SVC, [SVC_KEYS], account_root=False)
    with_provider("a")(cluster, aws)
    aws.buckets.update({"aviato-a", "aviato-b"})
    aws.user_policies[SVC] = {"tenant": json.dumps(POLICY)}


def suspended_root(cluster, aws):
    cluster.users[(ACCOUNT, ROOT)]["suspended"] = 1


class Scenarios(ConvergeCase):

    def test_greenfield_tenant(self):
        for release in RELEASES:
            with self.subTest(release=release):
                cluster, aws = self.converge("greenfield tenant", PROVISION, no_seed, release)
                self.assertEqual(sorted(aws.buckets), ["aviato-a", "aviato-b"])
                self.assertIn((ACCOUNT, SVC), cluster.users)

    def test_teardown(self):
        for release in RELEASES:
            with self.subTest(release=release):
                cluster, _ = self.converge("teardown", TEARDOWN, provisioned, release)
                self.assertEqual(cluster.accounts, {})

    def test_converged_tenant_is_left_alone(self):
        for release in RELEASES:
            with self.subTest(release=release):
                cluster, aws = fresh(provisioned, release)
                self.assertEqual(self.run_all("converged", PROVISION, cluster, aws,
                                              check_mode=True),
                                 [False] * len(PROVISION))

    def test_check_passes_when_the_root_user_was_suspended_by_hand(self):
        """rgw_account_user would enable the user; --check of the S3/IAM steps after it
        meets the refusal of a suspended user and reports the step as not probed.

        Only whether a step fails is asserted."""
        for release in RELEASES:
            cluster, aws = fresh(seeded(provisioned, suspended_root), release)
            for name, params in PROVISION:
                res = run_module(importlib.import_module(name), params, cluster, aws,
                                 check_mode=True)
                self.assertFalse(res.failed, "%s %s: %s"
                                 % (release, name, res.kwargs.get("msg")))


if __name__ == "__main__":
    unittest.main()
