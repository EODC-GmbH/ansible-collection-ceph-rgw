#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""Record how this cluster's radosgw-admin, ceph and S3/IAM endpoint answer.

Test-only module for capture.yml. It creates a throwaway account, runs the commands the
collection's modules send plus the S3/IAM calls they make, and removes the account again.
tests/unit/test_fake_matches_capture.py replays the ordered transcript against the in-memory
cluster, so the fake is checked against a real release. Generated keys are replaced by fixed
placeholders everywhere in the transcript.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json
import secrets
import string

from ansible.module_utils.basic import AnsibleModule
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw import (
    RGW, RGW_COMMON_ARGS,
)

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False

# The placeholders the replay uses. The unknown key is generated too: a fixed one could
# exist on the cluster.
HELD = ("AKCAPTUREHELD0000000", "SKCAPTUREHELD0000000000000000000000000000")
UNKNOWN = ("AKCAPTUREUNKNOWN0000", "SKCAPTUREUNKNOWN000000000000000000000000")
UID = "captureroot"
ROLE = "capture-reader"
POLICY = {"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow", "Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::capture/*"]}]}


def generated_pair():
    alphabet = string.ascii_uppercase + string.digits
    return ("".join(secrets.choice(alphabet) for _ in range(20)),
            secrets.token_urlsafe(30)[:40])


def cli_steps(account):
    """(name, binary, args) in order. Reads of the gone account come after `account rm`."""
    acct = ["--account-id", account]
    user = ["--uid", UID] + acct
    role = ["--role-name", ROLE] + acct
    trust = json.dumps({"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Action": ["sts:AssumeRoleWithWebIdentity"],
        "Principal": {"Federated": [
            "arn:aws:iam::%s:oidc-provider/idp/realms/capture" % account]}}]})
    return {
        "setup": [
            ("account create", "radosgw-admin",
             ["account", "create"] + acct + ["--account-name", "capture-raviga"]),
            ("user create", "radosgw-admin",
             ["user", "create"] + user + ["--display-name", UID, "--account-root",
                                          "--access-key", HELD[0], "--secret-key", HELD[1]]),
            ("role create", "radosgw-admin",
             ["role", "create"] + role + ["--assume-role-policy-doc", trust]),
            ("role-policy put", "radosgw-admin",
             ["role-policy", "put"] + role + ["--policy-name", "tenant",
                                              "--policy-doc", json.dumps(POLICY)]),
        ],
        "reads": [
            ("account list", "radosgw-admin", ["account", "list"]),
            ("account get", "radosgw-admin", ["account", "get"] + acct),
            ("user list", "radosgw-admin", ["user", "list"] + acct),
            ("user info", "radosgw-admin", ["user", "info"] + user),
            ("user info by held key", "radosgw-admin",
             ["user", "info", "--access-key", HELD[0]]),
            ("user info by unknown key", "radosgw-admin",
             ["user", "info", "--access-key", UNKNOWN[0]]),
            ("user info of a missing uid", "radosgw-admin",
             ["user", "info", "--uid", "capturenobody"] + acct),
            ("role list", "radosgw-admin", ["role", "list"] + acct),
            ("oidc-provider list", "radosgw-admin", ["oidc-provider", "list"] + acct),
            ("config get", "ceph", ["config", "get", "client.rgw", "rgw_s3_auth_use_sts"]),
        ],
        "suspend": [("user suspend", "radosgw-admin", ["user", "suspend"] + user)],
        "enable": [("user enable", "radosgw-admin", ["user", "enable"] + user)],
        "teardown": [
            ("role-policy delete", "radosgw-admin",
             ["role-policy", "delete"] + role + ["--policy-name", "tenant"]),
            ("role delete", "radosgw-admin", ["role", "delete"] + role),
            ("user rm", "radosgw-admin", ["user", "rm"] + user),
            ("account rm", "radosgw-admin", ["account", "rm"] + acct),
        ],
        "gone": [
            ("account get of a gone account", "radosgw-admin", ["account", "get"] + acct),
            ("user list of a gone account", "radosgw-admin", ["user", "list"] + acct),
            ("role list of a gone account", "radosgw-admin", ["role", "list"] + acct),
            ("user info by a gone key", "radosgw-admin",
             ["user", "info", "--access-key", HELD[0]]),
            ("user info in a gone account", "radosgw-admin", ["user", "info"] + user),
            ("user suspend of a gone user", "radosgw-admin", ["user", "suspend"] + user),
            ("user rm of a gone user", "radosgw-admin", ["user", "rm"] + user),
            ("key create for a gone user", "radosgw-admin",
             ["key", "create"] + user + ["--access-key", HELD[0], "--secret-key", HELD[1]]),
            ("key rm of a gone user", "radosgw-admin",
             ["key", "rm"] + user + ["--access-key", HELD[0]]),
            ("role-policy put on a gone role", "radosgw-admin",
             ["role-policy", "put"] + role + ["--policy-name", "tenant",
                                              "--policy-doc", json.dumps(POLICY)]),
            ("role delete of a gone role", "radosgw-admin", ["role", "delete"] + role),
            ("account modify of a gone account", "radosgw-admin",
             ["account", "modify"] + acct + ["--max-roles", "5"]),
            ("account rm of a gone account", "radosgw-admin", ["account", "rm"] + acct),
        ],
    }


# (service, call, params) for each credential state.
AWS_CALLS = [
    ("s3", "list_buckets", {}),
    ("s3", "head_bucket", {"Bucket": "capture-none"}),
    ("iam", "list_open_id_connect_providers", {}),
    ("iam", "list_user_policies", {"UserName": UID}),
]


class Recorder(object):

    def __init__(self, module, real_held, real_unknown):
        self.module = module
        self.runners = {b: RGW(module, b) for b in ("radosgw-admin", "ceph")}
        self.swaps = [(real_held[0], HELD[0]), (real_held[1], HELD[1]),
                      (real_unknown[0], UNKNOWN[0]), (real_unknown[1], UNKNOWN[1])]
        self.real = {HELD: real_held, UNKNOWN: real_unknown}
        self.steps = []

    def hide(self, text):
        for real, placeholder in self.swaps:
            text = text.replace(real, placeholder)
        return text

    def reveal(self, args):
        out = []
        for arg in args:
            for real, placeholder in self.swaps:
                arg = arg.replace(placeholder, real)
            out.append(arg)
        return out

    def cli(self, steps):
        for name, binary, args in steps:
            rc, _, out, err = self.runners[binary].run(*self.reveal(args), json_out=False,
                                                      check_rc=False)
            self.steps.append(dict(name=name, via=binary, argv=args, rc=rc,
                                   stdout=self.hide(out), stderr=self.hide(err)))

    def aws(self, label, pair):
        real = self.real[pair]
        for service, call, params in AWS_CALLS:
            client = boto3.client(
                service, endpoint_url=self.module.params["endpoint_url"],
                region_name="us-east-1", config=Config(signature_version="s3v4"),
                aws_access_key_id=real[0], aws_secret_access_key=real[1],
                verify=self.module.params["validate_certs"])
            step = dict(name="%s with %s" % (call, label), via=service, call=call,
                        params=params, access_key=pair[0])
            try:
                getattr(client, call)(**params)
                step.update(code="", status=200)
            except ClientError as exc:
                response = exc.response or {}
                step.update(code=(response.get("Error") or {}).get("Code") or "",
                            status=(response.get("ResponseMetadata") or {})
                            .get("HTTPStatusCode"))
            self.steps.append(step)


def main():
    argument_spec = dict(
        account_id=dict(type="str", default="RGW90000000000000001"),
        endpoint_url=dict(type="str", required=True),
        validate_certs=dict(type="bool", default=True),
    )
    argument_spec.update(RGW_COMMON_ARGS)
    module = AnsibleModule(argument_spec=argument_spec)
    if not HAS_BOTO3:
        module.fail_json(msg="boto3 is required on the capture host")

    account = module.params["account_id"]
    recorder = Recorder(module, generated_pair(), generated_pair())
    _, _, out, _ = recorder.runners["radosgw-admin"].run("account", "list")
    if account in (json.loads(out) if out.strip() else []):
        module.fail_json(msg="account %s already exists; capture only uses an account it "
                             "creates itself, so remove it or pass another account_id"
                             % account)
    _, _, version, _ = recorder.runners["ceph"].run("version", json_out=False)

    steps = cli_steps(account)
    try:
        recorder.cli(steps["setup"])
        recorder.cli(steps["reads"])
        recorder.aws("an active held key", HELD)
        recorder.aws("an unknown key", UNKNOWN)
        recorder.cli(steps["suspend"])
        recorder.aws("a suspended held key", HELD)
        recorder.cli(steps["enable"])
    finally:
        recorder.cli(steps["teardown"])
    recorder.cli(steps["gone"])

    version = version.strip().splitlines()[-1]
    module.exit_json(changed=True, version=version, release=version.split()[-2],
                     account_id=account, steps=recorder.steps)


if __name__ == "__main__":
    main()
