# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""Converge-twice and dry-run contracts for every module that writes, per Ceph release.

Each CONVERGE row seeds a cluster and runs the four steps of converge_contract.

GREENFIELD rows are --check only, with the account or its root user missing: the dry run
reports the change and does not fail. S3 and IAM refuse a missing root user's keys;
radosgw-admin shows no user holds them. test_rgw_refusal covers a held key.

rgw_orch_restart is left out: a restart is an action, changed on every real run.

Run:  python3 -m unittest discover -s tests/unit
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import importlib
import json
import unittest

from ceph_releases import RELEASES
from converge_contract import (
    ROOT, VAULT, ConvergeCase, account, fresh, no_seed, root, seeded, writes,
)
from fake_aws import ACCOUNT, run_module
from test_rgw_iam import make_spec

STALE = ("AKSTALE", "SKSTALE")
TRUST = {"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow", "Action": ["sts:AssumeRoleWithWebIdentity"],
    "Principal": {"Federated": ["arn:aws:iam::%s:oidc-provider/idp/realms/r" % ACCOUNT]}}]}
POLICY = {"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow", "Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::b/*"]}]}
OTHER = {"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow", "Action": ["s3:PutObject"], "Resource": ["arn:aws:s3:::b/*"]}]}
URL = "https://idp/realms/r"
OIDC_ARN = "arn:aws:iam::%s:oidc-provider/idp/realms/r" % ACCOUNT
S3 = dict(endpoint_url="http://rgw", access_key=VAULT[0], secret_key=VAULT[1])
QUOTA = {"enabled": True, "max_size": 1024, "max_objects": 10}


def user(**kw):
    return dict(uid=ROOT, account_id=ACCOUNT, access_key=VAULT[0],
                secret_key=VAULT[1], **kw)


def role(**kw):
    return dict(name="reader", account_id=ACCOUNT, assume_role_policy_document=TRUST,
                policies=[{"name": "tenant", "document": POLICY}], **kw)


def oidc(**kw):
    return dict(S3, url=URL, client_ids=["ceph"], thumbprints=["a" * 40], **kw)


def limited_spec():
    spec = make_spec()
    spec["tenants"][0]["limits"] = {"max_roles": 5}
    return spec


def holding(*pairs):
    return lambda c, a: c.add_user(ACCOUNT, ROOT, list(pairs))


def with_role(trust, policies):
    return lambda c, a: c.add_role(ACCOUNT, "reader", trust, policies)


def svc_user(policies):
    def seed(c, a):
        c.add_user(ACCOUNT, "svc", account_root=False)
        a.user_policies["svc"] = dict(policies)
    return seed


def with_bucket(c, a):
    a.buckets.add("aviato-a")


def with_provider(thumbprint):
    return lambda c, a: a.providers.update(
        {OIDC_ARN: {"ClientIDList": ["ceph"], "ThumbprintList": [thumbprint * 40]}})


# (label, module, params, seed, first run changes)
CONVERGE = [
    ("account created", "rgw_account", dict(account_id=ACCOUNT, name="tenant-aviato"),
     no_seed, True),
    ("account quota drift", "rgw_account", dict(account_id=ACCOUNT, quota=QUOTA),
     account, True),
    ("account renamed", "rgw_account",
     dict(account_id=ACCOUNT, name="tenant-new", update_name=True), account, True),
    ("account in place", "rgw_account", dict(account_id=ACCOUNT, name="tenant-aviato"),
     account, False),
    ("account removed", "rgw_account", dict(account_id=ACCOUNT, state="absent"),
     account, True),
    ("account already gone", "rgw_account", dict(account_id=ACCOUNT, state="absent"),
     no_seed, False),
    ("user created", "rgw_account_user", user(), account, True),
    ("user keys rotated", "rgw_account_user", user(rotate_keys=True),
     seeded(account, holding(STALE)), True),
    ("user stale key pruned", "rgw_account_user", user(rotate_keys=True),
     seeded(account, holding(VAULT, STALE)), True),
    ("user suspended", "rgw_account_user", user(suspended=True), root, True),
    ("user removed", "rgw_account_user", user(state="absent"), root, True),
    ("role created", "rgw_role", role(), account, True),
    ("role trust drift", "rgw_role", role(),
     seeded(account, with_role({}, {"tenant": POLICY})), True),
    ("role policy drift and purge", "rgw_role", role(),
     seeded(account, with_role(TRUST, {"tenant": OTHER, "stale": POLICY})), True),
    ("role duration drift", "rgw_role", role(max_session_duration=7200),
     seeded(account, with_role(TRUST, {"tenant": POLICY})), True),
    ("role removed", "rgw_role", dict(name="reader", account_id=ACCOUNT, state="absent"),
     seeded(account, with_role(TRUST, {"tenant": POLICY})), True),
    ("config value", "rgw_config", dict(option="rgw_s3_auth_use_sts", value="true"),
     lambda c, a: c.config.update({("client.rgw", "rgw_s3_auth_use_sts"): "false"}), True),
    ("config secret", "rgw_config", dict(option="rgw_sts_key", secret_value="k" * 16),
     no_seed, True),
    ("iam roles created", "rgw_iam_sync", dict(spec=make_spec()),
     lambda c, a: c.add_account("RGW1"), True),
    ("iam account limits", "rgw_iam_sync", dict(spec=limited_spec()),
     lambda c, a: c.add_account("RGW1", max_roles=1000), True),
    ("buckets created", "rgw_bucket", dict(S3, names=["aviato-a", "aviato-b"]),
     seeded(root, with_bucket), True),
    ("bucket removed", "rgw_bucket", dict(S3, name="aviato-a", state="absent"),
     seeded(root, with_bucket), True),
    ("oidc created", "rgw_oidc_provider", oidc(), root, True),
    ("oidc thumbprint drift", "rgw_oidc_provider", oidc(),
     seeded(root, with_provider("b")), True),
    ("oidc removed", "rgw_oidc_provider", oidc(state="absent"),
     seeded(root, with_provider("a")), True),
    ("user policy put", "rgw_user_policy", dict(S3, user_name="svc", policy=POLICY),
     seeded(root, svc_user({})), True),
    ("user policy drift and prune", "rgw_user_policy",
     dict(S3, user_name="svc", policy=POLICY, prune=True),
     seeded(root, svc_user({"tenant": json.dumps(OTHER), "old": json.dumps(POLICY)})),
     True),
]

# (label, module, params, seed)
GREENFIELD = [
    ("user in a missing account", "rgw_account_user", user(), no_seed),
    ("role in a missing account", "rgw_role", role(), no_seed),
    ("iam roles in a missing account", "rgw_iam_sync", dict(spec=limited_spec()), no_seed),
    ("buckets before the root user", "rgw_bucket", dict(S3, name="aviato-a"), no_seed),
    ("oidc before the root user", "rgw_oidc_provider", oidc(), no_seed),
    ("user policy before the user", "rgw_user_policy",
     dict(S3, user_name="svc", policy=POLICY), root),
    ("user policy before the root user", "rgw_user_policy",
     dict(S3, user_name="svc", policy=POLICY), no_seed),
]


class Converge(ConvergeCase):

    def converge_row(self, label, name, params, seed, first_changes, release):
        self.converge(label, [(name, params)], seed, release, first_changes)

    def test_converge_rows(self):
        for release in RELEASES:
            for row in CONVERGE:
                with self.subTest(release=release, row=row[0]):
                    self.converge_row(*row, release=release)

    def test_greenfield_rows(self):
        for release in RELEASES:
            for label, name, params, seed in GREENFIELD:
                with self.subTest(release=release, row=label):
                    cluster, aws = fresh(seed, release)
                    res = run_module(importlib.import_module(name), params, cluster, aws,
                                     check_mode=True)
                    self.assertFalse(res.failed, res.kwargs.get("msg"))
                    self.assertTrue(res.kwargs["changed"])
                    self.assertEqual(writes(cluster, aws), [], "check mode wrote")


if __name__ == "__main__":
    unittest.main()
