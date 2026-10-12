# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""Static-key service users: composition, validation and the inline-policy reconcile.

No cluster and no boto3: the IAM client is a fake that records what was sent.

Run:  python3 -m unittest discover -s tests/unit
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json
import unittest

from test_rgw_oidc_replace import _bind_fqcn, _stub_boto3

_bind_fqcn()
_stub_boto3()

from botocore.exceptions import ClientError  # noqa: E402

from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_iam import (  # noqa: E402
    IAMError, SpecError, compose_users, permission_document, same, service_user_name,
)
from rgw_user_policy import policy_from_api, reconcile_user_policy  # noqa: E402


def make_spec(users):
    return {
        "delegated_infix": "x",
        "capabilities": {
            "objects-read": {"scope": "buckets",
                             "actions": ["s3:GetObject", "s3:ListBucket"]},
            "bucket-create": {"scope": "buckets", "actions": ["s3:CreateBucket"]},
            "account-list": {"scope": "account", "actions": ["s3:ListAllMyBuckets"]},
            "iam-delegate": {"scope": "delegated-iam", "actions": ["iam:CreateRole"]},
        },
        "baseline": ["account-list"],
        "tenants": [{"name": "aviato-data", "account_id": "RGW1", "users": users}],
    }


class ComposeUsers(unittest.TestCase):

    def test_name_is_the_documented_convention(self):
        self.assertEqual(service_user_name("aviato-data", "backup"), "iam-aviato-data-svc-backup")

    def test_scoped_user_gets_the_role_shaped_policy(self):
        users = compose_users(make_spec([{"name": "backup", "capabilities": ["objects-read"],
                                          "buckets": ["aviato-data-logs"]}]))
        self.assertEqual(len(users), 1)
        user = users[0]
        self.assertEqual(user["name"], "iam-aviato-data-svc-backup")
        self.assertEqual(user["capabilities"], ["account-list", "objects-read"])
        resources = [r for s in user["policy"]["Statement"] for r in s["Resource"]]
        self.assertIn("arn:aws:s3:::aviato-data-logs", resources)
        self.assertIn("arn:aws:s3:::aviato-data-logs/*", resources)
        self.assertNotIn("arn:aws:s3:::aviato-data-*", resources)

    def test_unscoped_user_defaults_to_the_tenant_prefix(self):
        user = compose_users(make_spec([{"name": "etl", "capabilities": ["objects-read"]}]))[0]
        self.assertEqual(user["buckets"], ["aviato-data-*"])

    def test_policy_matches_a_role_with_the_same_capabilities(self):
        spec = make_spec([{"name": "etl", "capabilities": ["objects-read"]}])
        user = compose_users(spec)[0]
        self.assertTrue(same(user["policy"], permission_document(spec, user)))

    def test_absent_users_compose_nothing(self):
        self.assertEqual(compose_users(make_spec(
            [{"name": "gone", "capabilities": ["nope"], "state": "absent"}])), [])

    def test_tenant_without_users(self):
        spec = make_spec([])
        del spec["tenants"][0]["users"]
        self.assertEqual(compose_users(spec), [])


REJECT_CASES = [
    ([{"name": "a", "capabilities": ["nope"]}], "an unknown capability"),
    ([{"name": "a", "capabilities": ["iam-delegate"]}], "delegated IAM on a static key"),
    ([{"name": "a", "capabilities": ["objects-read"], "buckets": ["hooli-data"]}],
     "another tenant's bucket"),
    ([{"name": "a", "capabilities": ["bucket-create"], "prefixes": ["p/*"]}],
     "prefixes combined with bucket-create"),
    ([{"name": "a", "capabilities": ["objects-read"]},
      {"name": "a", "capabilities": ["objects-read"]}], "a duplicate user"),
    ([{"name": "n" * 60, "capabilities": ["objects-read"]}], "a name over 64 characters"),
    ([{"name": "bad:name", "capabilities": ["objects-read"]}], "a character IAM rejects"),
]


class RejectUsers(unittest.TestCase):

    def test_rejections(self):
        for users, why in REJECT_CASES:
            with self.subTest(why=why):
                with self.assertRaises(SpecError):
                    compose_users(make_spec(users))


class FakeIAM:
    def __init__(self, policies=None, exists=True, as_text=False):
        self.policies = dict(policies or {})
        self.exists = exists
        self.as_text = as_text
        self.puts = []
        self.deletes = []

    def list_user_policies(self, UserName):
        if not self.exists:
            raise ClientError({"Error": {"Code": "NoSuchEntity"}}, "ListUserPolicies")
        return {"PolicyNames": sorted(self.policies)}

    def get_user_policy(self, UserName, PolicyName):
        doc = self.policies[PolicyName]
        return {"PolicyDocument": json.dumps(doc) if self.as_text else doc}

    def put_user_policy(self, UserName, PolicyName, PolicyDocument):
        self.puts.append((PolicyName, json.loads(PolicyDocument)))

    def delete_user_policy(self, UserName, PolicyName):
        self.deletes.append(PolicyName)


DOC = {"Version": "2012-10-17", "Statement": [
    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket"],
     "Resource": ["arn:aws:s3:::b", "arn:aws:s3:::b/*"]}]}


class ReconcileUserPolicy(unittest.TestCase):

    def test_puts_a_missing_policy(self):
        iam = FakeIAM()
        changed, actions, before, _ = reconcile_user_policy(iam, "u", "tenant", DOC)
        self.assertTrue(changed)
        self.assertIsNone(before)
        self.assertEqual(iam.puts, [("tenant", DOC)])

    def test_equal_policy_in_another_order_is_not_drift(self):
        reordered = {"Statement": [dict(DOC["Statement"][0], Action=["s3:ListBucket",
                                                                     "s3:GetObject"])],
                     "Version": "2012-10-17"}
        for as_text in (False, True):
            with self.subTest(as_text=as_text):
                iam = FakeIAM({"tenant": reordered}, as_text=as_text)
                changed, actions, _, _ = reconcile_user_policy(iam, "u", "tenant", DOC)
                self.assertFalse(changed)
                self.assertEqual(iam.puts, [])
                self.assertTrue(actions[0].startswith("ok"))

    def test_check_mode_reports_without_writing(self):
        iam = FakeIAM({"tenant": {"Version": "2012-10-17", "Statement": []}})
        changed, actions, _, _ = reconcile_user_policy(iam, "u", "tenant", DOC, check_mode=True)
        self.assertTrue(changed)
        self.assertEqual(iam.puts, [])
        self.assertTrue(actions[0].startswith("WOULD"))

    def test_check_mode_tolerates_a_user_not_created_yet(self):
        iam = FakeIAM(exists=False)
        changed, _, _, _ = reconcile_user_policy(iam, "u", "tenant", DOC, check_mode=True)
        self.assertTrue(changed)

    def test_a_missing_user_fails_a_real_run(self):
        with self.assertRaises(IAMError):
            reconcile_user_policy(FakeIAM(exists=False), "u", "tenant", DOC)

    def test_extra_policy_is_reported_not_removed(self):
        iam = FakeIAM({"tenant": DOC, "handmade": DOC})
        changed, actions, _, _ = reconcile_user_policy(iam, "u", "tenant", DOC)
        self.assertFalse(changed)
        self.assertEqual(iam.deletes, [])
        self.assertTrue(any(a.startswith("EXTRA") for a in actions))

    def test_extra_policy_is_removed_under_prune(self):
        iam = FakeIAM({"tenant": DOC, "handmade": DOC})
        changed, _, _, _ = reconcile_user_policy(iam, "u", "tenant", DOC, prune=True)
        self.assertTrue(changed)
        self.assertEqual(iam.deletes, ["handmade"])


class PolicyFromApi(unittest.TestCase):

    def test_forms(self):
        self.assertEqual(policy_from_api(DOC), DOC)
        self.assertEqual(policy_from_api(json.dumps(DOC)), DOC)
        self.assertEqual(policy_from_api('%7B%22Version%22%3A%20%222012-10-17%22%7D'),
                         {"Version": "2012-10-17"})


if __name__ == "__main__":
    unittest.main()
