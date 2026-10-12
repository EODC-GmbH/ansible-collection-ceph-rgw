#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: rgw_role_info
short_description: Read-only inventory of the IAM roles in a Ceph RGW account
version_added: "0.3.0"
description:
  - Reports the IAM roles of one RGW account - ARN, session duration, trust document,
    inline policy names and documents, attached managed policy ARNs and tags - using only
    read-only C(radosgw-admin) subcommands.
  - C(role list --account-id) returns all of that in one call. C(role-policy list) is
    used only for a role whose listing entry carries no C(PermissionPolicies).
options:
  account_id:
    description: The RGW account whose roles are reported.
    type: str
    required: true
  name:
    description: Report only this role. Mutually exclusive with O(name_prefix).
    type: str
  name_prefix:
    description:
      - Report every role whose name starts with this string.
      - A tenant's roles, platform and self-minted alike, are exactly C(<tenant>-*).
    type: str
  exec_mode:
    description: How to reach C(radosgw-admin).
    type: str
    choices: [auto, native, container, cephadm]
    default: auto
  fsid:
    description: Cluster fsid. Read from C(/etc/ceph/ceph.conf) when not given.
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
author:
  - Underknowledge (@Underknowledge)
notes:
  - A role that RV(roles[].tags) reports as tagged is unassumable. Role tags become the
    session's principal tags, so every assume also needs C(sts:TagSession), and
    C(UntagRole) is a silent no-op on this build. Delete and recreate the role.
"""

EXAMPLES = r"""
- name: Inventory every role in the account
  underknowledge.ceph_rgw.rgw_role_info:
    account_id: RGW00000000000000001
  register: rgw_roles

- name: Find every role belonging to one tenant, for teardown
  underknowledge.ceph_rgw.rgw_role_info:
    account_id: RGW00000000000000001
    name_prefix: aviato-
  register: rgw_aviato_roles

- name: Remove them, policies first
  underknowledge.ceph_rgw.rgw_role:
    name: "{{ item.name }}"
    account_id: RGW00000000000000001
    state: absent
  loop: "{{ rgw_aviato_roles.roles }}"
  loop_control:
    label: "{{ item.name }}"

- name: Read one role's trust document
  underknowledge.ceph_rgw.rgw_role_info:
    account_id: RGW00000000000000001
    name: aviato-reader
  register: rgw_reader
"""

RETURN = r"""
roles:
  description: One entry per role reported, sorted by name.
  returned: always
  type: list
  elements: dict
  contains:
    name:
      description: The role name.
      type: str
    role_id:
      description: The immutable role id. Changes only if the role is recreated.
      type: str
    arn:
      description: The role ARN that trust policies and AssumeRole calls reference.
      type: str
    path:
      description: The IAM path of the role.
      type: str
    create_date:
      description: When the role was created, as RGW reports it.
      type: str
    max_session_duration:
      description: Assumed-session lifetime in seconds, or V(0) when the build omits it.
      type: int
    assume_role_policy_document:
      description: The trust policy, parsed. An empty dict when it cannot be parsed.
      type: dict
    policy_names:
      description: Names of the inline permission policies attached to the role.
      type: list
      elements: str
    policies:
      description:
        - The inline permission policies, each as C(name) plus the parsed C(document).
        - Empty when the documents were not carried in the listing.
      type: list
      elements: dict
    managed_policy_arns:
      description: ARNs of the managed policies attached to the role.
      type: list
      elements: str
    tags:
      description: Role tags. Any entry here means the role cannot be assumed.
      type: list
      elements: dict
  sample:
    - name: aviato-reader
      arn: "arn:aws:iam::RGW00000000000000001:role/aviato-reader"
      max_session_duration: 3600
      policy_names: [tenant]
      managed_policy_arns: []
exec_mode_used:
  description: Which execution path actually ran.
  returned: always
  type: str
  sample: container
"""

from ansible.module_utils.basic import AnsibleModule
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw import (
    RGW, RGW_COMMON_ARGS, RGWError,
)
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_discovery import (
    inline_policies, role_entries,
)
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_iam import loads


def policy_names(parsed):
    """Policy names out of `role-policy list`, whatever shape this build returns."""
    if isinstance(parsed, dict):
        parsed = parsed.get("PolicyNames") or []
    out = []
    for item in parsed or []:
        name = item if isinstance(item, str) else (item.get("PolicyName")
                                                   or item.get("policyname") or "")
        if name:
            out.append(name)
    return sorted(out)


def describe(rgw, name, entry, account_id):
    """One role's report. Costs a `role-policy list` only when the entry carries none."""
    policies = sorted(inline_policies(entry).items())
    if "PermissionPolicies" in entry:
        names = [pname for pname, _ in policies]
    else:
        _, parsed, _, _ = rgw.run("role-policy", "list", "--role-name", name,
                                  "--account-id", account_id, check_rc=False)
        names = policy_names(parsed)
    return {
        "name": name,
        "role_id": entry.get("RoleId", ""),
        "arn": entry.get("Arn", ""),
        "path": entry.get("Path", ""),
        "create_date": entry.get("CreateDate", ""),
        "max_session_duration": int(entry.get("MaxSessionDuration") or 0),
        "assume_role_policy_document": loads(entry.get("AssumeRolePolicyDocument")) or {},
        "policy_names": names,
        "policies": [{"name": pname, "document": doc} for pname, doc in policies],
        "managed_policy_arns": sorted(entry.get("ManagedPermissionPolicies") or []),
        "tags": entry.get("Tags") or [],
    }


def main():
    argument_spec = dict(
        account_id=dict(type="str", required=True),
        name=dict(type="str"),
        name_prefix=dict(type="str"),
    )
    argument_spec.update(RGW_COMMON_ARGS)

    module = AnsibleModule(
        argument_spec=argument_spec,
        mutually_exclusive=[("name", "name_prefix")],
        supports_check_mode=True,
    )

    p = module.params
    account_id = p["account_id"]
    rgw = RGW(module)

    try:
        _, listing, _, _ = rgw.run("role", "list", "--account-id", account_id)
        roles = []
        for name, entry in role_entries(listing):
            if p["name"] is not None and name != p["name"]:
                continue
            if p["name_prefix"] is not None and not name.startswith(p["name_prefix"]):
                continue
            roles.append(describe(rgw, name, entry, account_id))
    except RGWError as exc:
        module.fail_json(msg="radosgw-admin failed listing roles in account %s: %s"
                             % (account_id, exc.msg),
                         rc=exc.rc, stderr=exc.stderr, cmd=exc.cmd)

    module.exit_json(changed=False, roles=roles, exec_mode_used=rgw.exec_mode_used)


if __name__ == "__main__":
    main()
