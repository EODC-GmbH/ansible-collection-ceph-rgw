# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""In-memory S3 and IAM clients for one account, and a runner for any module's main().

The clients keep state across module runs, so a second run sees what the first wrote. Every
mutating call is logged in `writes`. Whether a call is refused follows from the fake cluster:
who holds the signing key, and whether that user is suspended.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import contextlib
import importlib
from unittest import mock

from test_rgw_oidc_replace import _bind_fqcn, _stub_boto3

_bind_fqcn()
_stub_boto3()

from ansible.module_utils import basic  # noqa: E402
from module_args import patch_module_args  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw import RGW  # noqa: E402

ACCOUNT = "RGW00000000000000001"


def client_error(code, status=400):
    return ClientError({"Error": {"Code": code},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, "op")


class FakeAWS(object):
    """S3 buckets, OIDC providers and IAM user inline policies.

    Providers are scoped to the signer's account through their ARN; the unit tests sign as
    ACCOUNT's root user.

    Calls are signed with the key the module built its client with. A key nobody in `cluster`
    holds, or one whose user is suspended, is refused with the code the cluster's release sends
    for that case. With denied set, a held key is refused too: a user without permission.
    """

    def __init__(self, cluster, denied=False):
        self.cluster = cluster
        self.denied = denied
        self.access_key = None
        self.buckets = set()
        self.foreign_buckets = set()
        self.providers = {}
        self.user_policies = {}
        self.writes = []

    def client(self, service, aws_access_key_id=None, **kwargs):
        self.access_key = aws_access_key_id
        return self

    def refusal(self):
        """Why the signing key is refused, as a key of the profile's `refused`, or None."""
        held = self.cluster.holder(self.access_key)
        if held is None:
            return "unknown key"
        if held[1]["suspended"]:
            return "suspended user"
        return "denied" if self.denied else None

    def signer_account(self):
        return self.cluster.holder(self.access_key)[0]

    def refuse(self):
        reason = self.refusal()
        if reason:
            raise client_error(*self.cluster.profile["refused"][reason])

    # S3
    def list_buckets(self):
        self.refuse()
        return {"Buckets": [{"Name": n} for n in sorted(self.buckets)]}

    def head_bucket(self, Bucket):
        if self.refusal():
            raise client_error("403", 403)
        if Bucket in self.foreign_buckets:
            raise client_error("403", 403)
        if Bucket not in self.buckets:
            raise client_error("404", 404)

    def create_bucket(self, Bucket):
        self.refuse()
        self.writes.append(("create_bucket", Bucket))
        self.buckets.add(Bucket)

    def delete_bucket(self, Bucket):
        self.refuse()
        self.writes.append(("delete_bucket", Bucket))
        self.buckets.remove(Bucket)

    # IAM: OIDC providers
    def list_open_id_connect_providers(self):
        self.refuse()
        prefix = "arn:aws:iam::%s:" % self.signer_account()
        return {"OpenIDConnectProviderList": [{"Arn": a} for a in sorted(self.providers)
                                              if a.startswith(prefix)]}

    def get_open_id_connect_provider(self, OpenIDConnectProviderArn):
        self.refuse()
        return dict(self.providers[OpenIDConnectProviderArn])

    def create_open_id_connect_provider(self, Url, ClientIDList, ThumbprintList):
        self.refuse()
        self.writes.append(("create_oidc", Url))
        arn = "arn:aws:iam::%s:oidc-provider/%s" % (self.signer_account(),
                                                     Url.split("://", 1)[-1])
        self.providers[arn] = {"ClientIDList": list(ClientIDList),
                               "ThumbprintList": list(ThumbprintList)}
        return {"OpenIDConnectProviderArn": arn}

    def delete_open_id_connect_provider(self, OpenIDConnectProviderArn):
        self.refuse()
        self.writes.append(("delete_oidc", OpenIDConnectProviderArn))
        del self.providers[OpenIDConnectProviderArn]

    # IAM: user inline policies.
    # IAM users are the RGW users of the signer's account, named by display name.
    def iam_user_exists(self, name):
        account = self.signer_account()
        return any(a == account and u["display_name"] == name
                   for (a, _), u in self.cluster.users.items())

    def list_user_policies(self, UserName):
        self.refuse()
        if not self.iam_user_exists(UserName):
            raise client_error("NoSuchEntity", 404)
        return {"PolicyNames": sorted(self.user_policies.get(UserName, {}))}

    def get_user_policy(self, UserName, PolicyName):
        self.refuse()
        return {"PolicyDocument": self.user_policies[UserName][PolicyName]}

    def put_user_policy(self, UserName, PolicyName, PolicyDocument):
        self.refuse()
        self.writes.append(("put_user_policy", UserName, PolicyName))
        self.user_policies.setdefault(UserName, {})[PolicyName] = PolicyDocument

    def delete_user_policy(self, UserName, PolicyName):
        self.refuse()
        self.writes.append(("delete_user_policy", UserName, PolicyName))
        del self.user_policies[UserName][PolicyName]


class ClusterRGW(RGW):
    """The real runner with the process call answered by `cluster`.

    run() still raises on rc, parses JSON and redacts.
    """

    cluster = None

    def _resolve(self):
        self._base, self._mode = [], "native"

    def _run_raw(self, argv):
        return ClusterRGW.cluster.handle([str(a) for a in argv])


class Exited(Exception):

    def __init__(self, failed, kwargs):
        super(Exited, self).__init__(kwargs.get("msg", ""))
        self.failed = failed
        self.kwargs = kwargs


def run_module(module, params, cluster, aws, check_mode=False, probe_holder=True):
    """Run module.main() against cluster and aws; returns the Exited it ended with.

    A module taking key_holder gets it as the roles pass it,
    from rgw_key_holder_info run against cluster first.
    A failed lookup is returned instead, since the role stops there.
    probe_holder=False runs the module without one.
    """
    if (probe_holder and "key_holder" in getattr(module, "KEY_HOLDER_ARGS", {})
            and "key_holder" not in params):
        probe = run_module(importlib.import_module("rgw_key_holder_info"),
                           dict(access_key=params["access_key"]), cluster, aws,
                           check_mode=check_mode)
        if probe.failed:
            return probe
        params = dict(params, key_holder=probe.kwargs["key_holder"])
    args = dict(params, _ansible_check_mode=check_mode, _ansible_diff=True)

    def leave(failed):
        def _exit(self, **kwargs):
            raise Exited(failed, kwargs)
        return _exit

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch_module_args(args))
        stack.enter_context(mock.patch.object(ClusterRGW, "cluster", cluster))
        stack.enter_context(mock.patch.object(basic.AnsibleModule, "exit_json", leave(False)))
        stack.enter_context(mock.patch.object(basic.AnsibleModule, "fail_json", leave(True)))
        if hasattr(module, "RGW"):
            stack.enter_context(mock.patch.object(module, "RGW", ClusterRGW))
        if hasattr(module, "boto3"):
            stack.enter_context(mock.patch.object(module.boto3, "client", aws.client))
        try:
            module.main()
        except Exited as exc:
            return exc
    raise AssertionError("module returned without exit_json")
