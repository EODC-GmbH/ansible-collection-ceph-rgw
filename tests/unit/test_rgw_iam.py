# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""Composition, validation and reconciliation, with no cluster and no radosgw-admin.

Run:  python3 -m unittest discover -s tests/unit
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json
import unittest

from test_rgw_oidc_replace import _bind_fqcn

_bind_fqcn()

from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_iam import (  # noqa: E402
    SESSION_DURATION_MIN, IAMError, SpecError, compose, norm, permission_document, same,
    trust_document,
)
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_iam_reconcile import (  # noqa: E402
    Reconciler, Runner,
)


def make_spec():
    return {
        "grant_claim": "ceph_grants", "delegated_infix": "x",
        "provider_host": "idp/realms/r",
        "capabilities": {
            "objects-read": {"scope": "buckets",
                             "actions": ["s3:GetObject", "s3:ListBucket"]},
            "bucket-create": {"scope": "buckets", "actions": ["s3:CreateBucket"]},
            "account-list": {"scope": "account", "actions": ["s3:ListAllMyBuckets"]},
            "iam-delegate": {"scope": "delegated-iam", "actions": ["iam:CreateRole"]},
        },
        "baseline": ["account-list"],
        "catalogue": [{"name": "reader", "capabilities": ["objects-read"]},
                      {"name": "iamadmin", "capabilities": ["iam-delegate"]}],
        "tenants": [{"name": "t1", "account_id": "RGW1", "roles": [
            {"name": "analytics", "capabilities": ["objects-read"],
             "buckets": ["t1-data"], "prefixes": ["teamA/*"]}]}],
    }


REJECT_CASES = [
    ({"name": "t1", "account_id": "RGW1",
      "roles": [{"name": "reader", "capabilities": ["objects-read"]}]},
     "a tenant role reusing a catalogue name"),
    ({"name": "t1", "account_id": "RGW1",
      "roles": [{"name": "x-mine", "capabilities": ["objects-read"]}]},
     "a tenant role in the delegated name space"),
    ({"name": "t1", "account_id": "RGW1",
      "roles": [{"name": "a", "capabilities": ["nope"]}]},
     "an unknown capability"),
    ({"name": "t1", "account_id": "RGW1",
      "roles": [{"name": "a", "capabilities": ["objects-read"],
                 "buckets": ["t2-data"]}]},
     "another tenant's bucket"),
    ({"name": "t1", "account_id": "RGW1",
      "roles": [{"name": "a", "capabilities": ["bucket-create"],
                 "prefixes": ["p/*"]}]},
     "prefixes combined with bucket-create"),
    ({"name": "t1", "account_id": "RGW1", "session_duration": 60},
     "a session shorter than RGW allows"),
    ({"name": "t1", "account_id": ""}, "a tenant with no account"),
    ({"name": "t" * 60, "account_id": "RGW1"}, "a role name over 64 characters"),
    ({"name": "bad_tenant", "account_id": "RGW1"}, "an underscore in a role name"),
    ({"name": "bad:tenant", "account_id": "RGW1"}, "a colon in a role name"),
    ({"name": "t1", "account_id": "RGW1", "limits": {"max_role": 5}},
     "a misspelled account limit"),
    ({"name": "t1", "account_id": "RGW1", "bucket_prefix": "t1data-",
      "roles": [{"name": "a", "capabilities": ["objects-read"],
                 "buckets": ["t1-x"]}]},
     "a bucket outside the tenant's own bucket_prefix"),
]


class FakeRGW(object):
    """Records every invocation; answers `role list` and `account get` from a script."""

    def __init__(self, roles=None, account=None):
        self.roles = roles if roles is not None else []
        self.account = account or {}
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("role", "list"):
            return 0, json.dumps(self.roles), ""
        if args[:2] == ("account", "get"):
            return 0, json.dumps(self.account), ""
        return 0, "", ""

    def must(self, what, *args):
        self.calls.append(args)
        return ""

    def verbs(self):
        return [c[:2] for c in self.calls]


def live_role(name, trust, perm, policy_name="tenant", duration=3600, tags=None):
    role = {"RoleName": name, "MaxSessionDuration": duration,
            "AssumeRolePolicyDocument": json.dumps(trust),
            "PermissionPolicies": [{"PolicyName": policy_name,
                                    "PolicyValue": json.dumps(perm)}]}
    if tags:
        role["Tags"] = tags
    return role


def live_from(spec, roles):
    return [live_role(r["name"], trust_document(spec, r), permission_document(spec, r),
                      duration=r["session_duration"]) for r in roles]


class TestComposition(unittest.TestCase):

    def setUp(self):
        self.spec = make_spec()

    def test_role_names_and_defaults(self):
        roles = compose(self.spec)
        self.assertEqual([r["name"] for r in roles],
                         ["t1-reader", "t1-iamadmin", "t1-analytics"])
        self.assertEqual(roles[0]["session_duration"], SESSION_DURATION_MIN)
        self.assertEqual(roles[0]["buckets"], ["t1-*"])

    def test_baseline_applies_to_catalogue_and_extra_roles(self):
        self.assertIn("account-list", compose(self.spec)[2]["capabilities"])

    def test_prefix_scoped_role_gets_a_prefix_condition(self):
        doc = permission_document(self.spec, compose(self.spec)[2])
        listing = [s for s in doc["Statement"] if "Condition" in s]
        self.assertTrue(listing)
        self.assertEqual(listing[0]["Condition"]["StringLike"]["s3:prefix"], ["teamA/*"])
        self.assertTrue(any(s["Resource"] == ["arn:aws:s3:::t1-data/teamA/*"]
                            for s in doc["Statement"]))

    def test_unscoped_role_covers_the_whole_bucket_prefix(self):
        doc = permission_document(self.spec, compose(self.spec)[0])
        self.assertIn(["arn:aws:s3:::t1-*", "arn:aws:s3:::t1-*/*"],
                      [s["Resource"] for s in doc["Statement"]])

    def test_delegated_iam_is_confined_to_the_tenant_namespace(self):
        doc = permission_document(self.spec, compose(self.spec)[1])
        self.assertTrue(any(s["Resource"] == ["arn:aws:iam::RGW1:role/t1-x-*"]
                            for s in doc["Statement"]))

    def test_trust_demands_this_roles_own_grant(self):
        trust = trust_document(self.spec, compose(self.spec)[0])
        cond = trust["Statement"][0]["Condition"]["ForAnyValue:StringEquals"]
        self.assertEqual(cond["idp/realms/r:ceph_grants"], "t1:reader")

    def test_bucket_prefix_drives_scoping_and_the_default(self):
        spec = dict(self.spec, tenants=[{
            "name": "t1", "account_id": "RGW1", "bucket_prefix": "t1data-",
            "roles": [{"name": "a", "capabilities": ["objects-read"],
                       "buckets": ["t1data-x"]}]}])
        composed = compose(spec)
        self.assertEqual(next(r for r in composed if r["role"] == "a")["buckets"],
                         ["t1data-x"])
        self.assertEqual(next(r for r in composed if r["role"] == "reader")["buckets"],
                         ["t1data-*"])

    def test_compose_rejects(self):
        for tenant, why in REJECT_CASES:
            with self.subTest(why=why):
                with self.assertRaises(SpecError):
                    compose(dict(self.spec, tenants=[tenant]))


class TestDocumentComparison(unittest.TestCase):

    def test_reordered_documents_compare_equal(self):
        a = {"Version": "1", "Statement": [{"Action": ["b", "a"], "Effect": "Allow"}]}
        b = {"Statement": [{"Effect": "Allow", "Action": ["a", "b"]}], "Version": "1"}
        self.assertTrue(same(a, b))
        self.assertEqual(norm(a), norm(b))

    def test_genuinely_different_documents_differ(self):
        self.assertFalse(same({"Action": ["a"]}, {"Action": ["a", "b"]}))


class TestRunner(unittest.TestCase):

    def test_must_raises_on_a_discarded_policy(self):
        """RGW exits 0 while silently discarding a policy with an unknown action."""
        rgw = Runner(lambda *a: (0, "is not a valid action: iam:Nope", ""))
        with self.assertRaises(IAMError):
            rgw.must("put policy", "role-policy", "put")

    def test_must_raises_on_a_parse_failure(self):
        rgw = Runner(lambda *a: (0, "", "failed to parse policy document"))
        with self.assertRaises(IAMError):
            rgw.must("put policy", "role-policy", "put")

    def test_must_returns_stdout_on_success(self):
        self.assertEqual(Runner(lambda *a: (0, "{}", "")).must("ok", "role", "list"), "{}")


class TestReconcile(unittest.TestCase):

    def setUp(self):
        self.spec = make_spec()
        self.roles = compose(self.spec)

    def test_noop_when_the_cluster_already_matches(self):
        fake = FakeRGW(roles=live_from(self.spec, self.roles), account={"max_roles": 100})
        spec = dict(self.spec, tenants=[dict(self.spec["tenants"][0], limits={})])
        rec = Reconciler(spec, fake)
        rec.run(self.roles)
        self.assertFalse(rec.changed)
        self.assertTrue(all(line.startswith("ok") for line in rec.actions), rec.actions)

    def test_creates_a_missing_role(self):
        rec = Reconciler(self.spec, FakeRGW(roles=[]))
        rec.run(self.roles)
        self.assertTrue(rec.changed)
        self.assertTrue(any("created" in line for line in rec.actions))

    def test_trust_drift_is_modified_in_place_not_recreated(self):
        stale = {"Version": "2012-10-17", "Statement": []}
        live = [live_role(r["name"], stale, permission_document(self.spec, r),
                          duration=r["session_duration"]) for r in self.roles]
        fake = FakeRGW(roles=live)
        Reconciler(self.spec, fake).run(self.roles)
        self.assertIn(("role-trust-policy", "modify"), fake.verbs())
        self.assertNotIn(("role", "delete"), fake.verbs())
        self.assertNotIn(("role", "create"), fake.verbs())

    def test_dry_run_changes_nothing(self):
        fake = FakeRGW(roles=[])
        rec = Reconciler(self.spec, fake, dry_run=True)
        rec.run(self.roles)
        self.assertTrue(rec.changed)
        writes = [c for c in fake.verbs()
                  if c not in (("role", "list"), ("account", "get"))]
        self.assertEqual(writes, [])

    def test_a_tagged_role_is_reported_broken_without_prune(self):
        live = [live_role(r["name"], trust_document(self.spec, r),
                          permission_document(self.spec, r),
                          duration=r["session_duration"],
                          tags=[{"Key": "k", "Value": "v"}]) for r in self.roles]
        rec = Reconciler(self.spec, FakeRGW(roles=live))
        rec.run(self.roles)
        self.assertTrue(any(line.startswith("BROKEN") for line in rec.actions),
                        rec.actions)

    def test_prune_rebuilds_a_tagged_role_delete_first_with_the_declared_duration(self):
        live = [live_role(r["name"], trust_document(self.spec, r),
                          permission_document(self.spec, r), duration=7200,
                          tags=[{"Key": "k", "Value": "v"}]) for r in self.roles]
        fake = FakeRGW(roles=live)
        Reconciler(dict(self.spec, prune=True), fake).run(self.roles[:1])
        verbs = fake.verbs()
        self.assertLess(verbs.index(("role", "delete")), verbs.index(("role", "create")))
        create = next(c for c in fake.calls if c[:2] == ("role", "create"))
        self.assertIn("--max-session-duration=%d" % self.roles[0]["session_duration"], create)
        self.assertNotIn(("role", "update"), verbs)

    def test_stale_platform_role_is_reported_but_kept_without_prune(self):
        live = live_from(self.spec, self.roles) + [live_role("t1-gone", {}, {})]
        fake = FakeRGW(roles=live)
        rec = Reconciler(self.spec, fake)
        rec.run(self.roles)
        self.assertTrue(any(line.startswith("STALE") and "t1-gone" in line
                            for line in rec.actions), rec.actions)
        self.assertNotIn(("role", "delete"), fake.verbs())

    def test_prune_removes_a_stale_platform_role(self):
        live = live_from(self.spec, self.roles) + [live_role("t1-gone", {}, {})]
        fake = FakeRGW(roles=live)
        Reconciler(dict(self.spec, prune=True), fake).run(self.roles)
        self.assertIn(("role", "delete"), fake.verbs())

    def test_prune_never_touches_the_tenants_own_namespace(self):
        live = live_from(self.spec, self.roles) + [live_role("t1-x-theirs", {}, {})]
        fake = FakeRGW(roles=live)
        Reconciler(dict(self.spec, prune=True), fake).run(self.roles)
        deleted = [c for c in fake.calls if c[:2] == ("role", "delete")]
        self.assertFalse(any("t1-x-theirs" in str(c) for c in deleted), deleted)


if __name__ == "__main__":
    unittest.main()
