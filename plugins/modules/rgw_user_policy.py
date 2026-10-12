#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: rgw_user_policy
short_description: Manage the inline permission policy of a Ceph RGW account user
version_added: "0.3.0"
description:
  - Puts one named inline policy on a plain (non-root) user inside an RGW account, and
    reports or removes any other inline policy on it.
  - This is what scopes a static-key service user. Without a policy an account user can
    do nothing at all; with one it can do exactly what the policy allows, inside its own
    account only.
options:
  user_name:
    description:
      - The IAM UserName, which RGW resolves against the user's B(display name), not its
        uid. Create the user with uid and display name equal, as
        M(underknowledge.ceph_rgw.rgw_account_user) does by default.
    type: str
    required: true
  policy_name:
    description: Name of the inline policy this module owns.
    type: str
    default: tenant
  policy:
    description: The desired policy document.
    type: dict
    required: true
  prune:
    description: Remove inline policies other than O(policy_name). Without it they are only reported.
    type: bool
    default: false
  endpoint_url:
    description: The RGW endpoint serving the IAM API.
    type: str
    required: true
  region:
    description: SigV4 signing region.
    type: str
    default: us-east-1
  access_key:
    description: S3 access key of the account-root user of the account holding O(user_name).
    type: str
    required: true
  secret_key:
    description: S3 secret key of that account-root user.
    type: str
    required: true
  validate_certs:
    description: Verify the endpoint's TLS certificate.
    type: bool
    default: true
  ca_bundle:
    description:
      - CA bundle to verify the endpoint's TLS certificate against, on the host running
        the module. Takes precedence over O(validate_certs) when set.
    type: path
  exec_mode:
    description:
      - How to reach C(radosgw-admin).
        The module calls it only after the IAM API refuses the keys,
        to see whether any user holds the access key.
    type: str
    choices: [auto, native, container, cephadm]
    default: auto
  fsid:
    description: Cluster fsid.
      Read from C(/etc/ceph/ceph.conf) when not given.
    type: str
  image:
    description: Ceph container image.
    type: str
  container_runtime:
    description: Container runtime to use.
    type: str
    choices: [docker, podman]
  cluster:
    description: Ceph cluster name.
    type: str
    default: ceph
  keyring:
    description: Path to a ceph keyring.
    type: str
requirements:
  - boto3
notes:
  - C(radosgw-admin) on squid can attach managed policies to a user but cannot put an
    inline one, and the six managed policies are all C(Resource "*"). The IAM API over
    boto3 is the only way to scope a user, which is why this module talks to
    O(endpoint_url).
  - RGW discards a whole policy that names one action it does not know, leaving the user
    with nothing. Compose O(policy) from the rgw_iam capability catalogue, not by hand.
  - In check mode a user that does not exist yet is reported as a pending put, because
    rgw_tenants creates it and a dry run ahead of that finds nothing.
  - RGW answers C(AccessDenied) both for an unknown key and for a user without permission.
    After a refusal the module runs C(radosgw-admin user info --access-key)
    to see whether any user holds the key,
    which needs the cluster admin keyring (use C(become)).
    If none does, check mode reports a pending put,
    since the account-root user is not created yet.
    Otherwise the task fails.
author:
  - Underknowledge (@Underknowledge)
"""

EXAMPLES = r"""
- name: Scope a service user to one bucket, read-only
  underknowledge.ceph_rgw.rgw_user_policy:
    user_name: iam-aviato-svc-backup
    policy:
      Version: "2012-10-17"
      Statement:
        - Effect: Allow
          Action: ["s3:GetObject", "s3:ListBucket"]
          Resource: ["arn:aws:s3:::aviato-data", "arn:aws:s3:::aviato-data/*"]
    endpoint_url: "{{ rgw_tenants_endpoint }}"
    access_key: "{{ vault_rgw_tenants_aviato_access_key }}"
    secret_key: "{{ vault_rgw_tenants_aviato_secret_key }}"
"""

RETURN = r"""
actions:
  description: One line per change, pending change, unchanged user or extra policy.
  returned: always
  type: list
  elements: str
  sample: ["CHANGED iam-aviato-svc-backup: policy 'tenant' put"]
"""

import json
from urllib.parse import unquote

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw import (
    RGW, RGW_COMMON_ARGS, diff_of,
)
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_iam import (
    IAMError, loads, same,
)
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_refusal import (
    REFUSED_CODES, error_code, exit_if_suspended_preview, key_holder, refused_msg,
)

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False


def policy_from_api(document):
    """GetUserPolicy's PolicyDocument as a dict, whichever form the client returned.

    botocore decodes the URL-encoded document AWS sends; RGW sends plain JSON, which
    arrives as a dict or as text depending on the botocore version.
    """
    if isinstance(document, dict):
        return document
    parsed = loads(document)
    if parsed is None:
        parsed = loads(unquote(document))
    return parsed or {}


def reconcile_user_policy(client, user, policy_name, desired, prune=False, check_mode=False):
    """Bring one user's inline policy to `desired`.

    Returns (changed, actions, before, after).
    Any inline policy other than `policy_name` is extra authority:
    always reported, removed only under prune.
    A ClientError other than NoSuchEntity is raised to the caller.
    """
    verb = "WOULD  " if check_mode else "CHANGED"
    try:
        names = client.list_user_policies(UserName=user).get("PolicyNames") or []
    except ClientError as exc:
        if error_code(exc) != "NoSuchEntity":
            raise
        if check_mode:
            # rgw_tenants creates the user, so a dry run ahead of it finds nothing.
            return True, [f"WOULD   {user}: policy '{policy_name}' put "
                          f"(user not created yet)"], None, desired
        raise IAMError(f"list policies of {user}: {exc}")

    before = None
    if policy_name in names:
        before = policy_from_api(client.get_user_policy(
            UserName=user, PolicyName=policy_name).get("PolicyDocument"))

    changed = before is None or not same(before, desired)
    if changed:
        actions = [f"{verb} {user}: policy '{policy_name}' put"]
        if not check_mode:
            client.put_user_policy(UserName=user, PolicyName=policy_name,
                                   PolicyDocument=json.dumps(desired))
    else:
        actions = [f"ok      {user}"]

    for stale in sorted(set(names) - {policy_name}):
        if not prune:
            actions.append(f"EXTRA   {user}: inline policy '{stale}' was not put here; "
                           f"set prune to remove it")
            continue
        changed = True
        actions.append(f"{verb} {user}: stale inline policy '{stale}' removed")
        if not check_mode:
            client.delete_user_policy(UserName=user, PolicyName=stale)

    return changed, actions, before, desired


def main():
    argument_spec = dict(
        user_name=dict(type="str", required=True),
        policy_name=dict(type="str", default="tenant"),
        policy=dict(type="dict", required=True),
        prune=dict(type="bool", default=False),
        endpoint_url=dict(type="str", required=True),
        region=dict(type="str", default="us-east-1"),
        access_key=dict(type="str", required=True, no_log=True),
        secret_key=dict(type="str", required=True, no_log=True),
        validate_certs=dict(type="bool", default=True),
        ca_bundle=dict(type="path"),
    )
    argument_spec.update(RGW_COMMON_ARGS)
    module = AnsibleModule(argument_spec=argument_spec, supports_check_mode=True)

    if not HAS_BOTO3:
        module.fail_json(msg=missing_required_lib("boto3"))

    p = module.params
    try:
        client = boto3.client(
            "iam",
            endpoint_url=p["endpoint_url"],
            region_name=p["region"],
            aws_access_key_id=p["access_key"],
            aws_secret_access_key=p["secret_key"],
            verify=p["ca_bundle"] or p["validate_certs"],
        )
    except (BotoCoreError, ValueError) as exc:
        module.fail_json(msg="could not build an IAM client for %s: %s"
                             % (p["endpoint_url"], exc))

    try:
        changed, actions, before, after = reconcile_user_policy(
            client, p["user_name"], p["policy_name"], p["policy"],
            prune=p["prune"], check_mode=module.check_mode)
    except ClientError as exc:
        code = error_code(exc)
        if code not in REFUSED_CODES:
            module.fail_json(msg="%s: %s" % (p["user_name"], exc))
        owner = key_holder(module, RGW(module), "the IAM request")
        exit_if_suspended_preview(module, owner, "the IAM request for %s" % p["user_name"],
                                  actions=["SKIP    %s: key holder suspended, not probed"
                                           % p["user_name"]])
        if owner is None and module.check_mode:
            # Greenfield tenant: the account-root user does not exist yet.
            greenfield = dict(diff=diff_of(None, p["policy"])) if module._diff else {}
            module.exit_json(changed=True, **greenfield, actions=[
                f"WOULD   {p['user_name']}: policy '{p['policy_name']}' put "
                f"(account-root user not created yet)"])
        module.fail_json(msg=refused_msg("the IAM request for %s" % p["user_name"],
                                         code, owner))
    except (IAMError, BotoCoreError) as exc:
        module.fail_json(msg="%s: %s" % (p["user_name"], exc))

    result = dict(changed=changed, actions=actions)
    if module._diff:
        result["diff"] = diff_of(before, after)
    module.exit_json(**result)


if __name__ == "__main__":
    main()
