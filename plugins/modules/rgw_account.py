#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: rgw_account
short_description: Manage a Ceph RGW account and its storage quota
version_added: "0.3.0"
description:
  - Creates or removes an RGW account, the tenant isolation boundary, and reconciles its
    account-scoped storage quota.
  - Account entity limits (C(max_users), C(max_roles) and so on) are left to
    C(rgw_iam_sync); two writers would each report the other's write as drift.
options:
  account_id:
    description: The account id, C(RGW) followed by 17 digits.
    type: str
    required: true
  name:
    description:
      - Account name, applied only when the account is created.
      - The name is cosmetic and not reconciled on an existing account unless O(update_name) is
        set.
    type: str
  update_name:
    description: Also reconcile O(name) on an account that already exists.
    type: bool
    default: false
  quota:
    description:
      - Account-scoped storage quota. Omit to leave the account's quota untouched.
    type: dict
    suboptions:
      enabled:
        description: Whether the quota is enforced.
        type: bool
        default: false
      max_size:
        description: Cap in bytes, or V(-1) for unlimited.
        type: int
        default: -1
      max_objects:
        description: Object-count cap, or V(-1) for unlimited.
        type: int
        default: -1
  state:
    description: Whether the account should exist.
    type: str
    choices: [present, absent]
    default: present
  purge_data:
    description:
      - Pass C(--purge-data) when removing, deleting the account's remaining data.
      - Without it, RGW refuses to remove a non-empty account.
    type: bool
    default: false
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
"""

EXAMPLES = r"""
- name: Ensure the tenant account exists with a 5 TiB cap
  underknowledge.ceph_rgw.rgw_account:
    account_id: RGW00000000000000001
    name: tenant-aviato
    quota:
      enabled: true
      max_size: "{{ 5 * 1024 ** 4 }}"
      max_objects: 1000000

- name: Decommission a tenant, data and all
  underknowledge.ceph_rgw.rgw_account:
    account_id: RGW00000000000000001
    state: absent
    purge_data: true
"""

RETURN = r"""
account:
  description: The account as it stands after the run.
  returned: when state is present
  type: dict
  sample:
    name: tenant-aviato
    quota: {enabled: true, max_size: 5497558138880, max_objects: 1000000}
actions:
  description: What the run did, or would do in check mode. Any of C(created), C(removed), C(renamed), C(quota).
  returned: always
  type: list
  elements: str
  sample: [created]
exec_mode_used:
  description: Which execution path actually ran.
  returned: always
  type: str
  sample: container
"""

from ansible.module_utils.basic import AnsibleModule
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw import (
    RGW, RGW_COMMON_ARGS, RGWError, diff_of,
)
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_read import read_account


def snapshot(info):
    if info is None:
        return {"present": False}
    quota = info.get("quota") or {}
    return {
        "present": True,
        "name": info.get("name", ""),
        "quota": {
            "enabled": bool(quota.get("enabled", False)),
            "max_size": int(quota.get("max_size", -1)),
            "max_objects": int(quota.get("max_objects", -1)),
        },
    }


def main():
    argument_spec = dict(
        account_id=dict(type="str", required=True),
        name=dict(type="str"),
        update_name=dict(type="bool", default=False),
        quota=dict(type="dict", options=dict(
            enabled=dict(type="bool", default=False),
            max_size=dict(type="int", default=-1),
            max_objects=dict(type="int", default=-1),
        )),
        state=dict(type="str", default="present", choices=["present", "absent"]),
        purge_data=dict(type="bool", default=False),
    )
    argument_spec.update(RGW_COMMON_ARGS)

    module = AnsibleModule(argument_spec=argument_spec, supports_check_mode=True)

    p = module.params
    account_id = p["account_id"]
    rgw = RGW(module)
    changed = False
    actions = []

    try:
        info = read_account(rgw, account_id)
        before = snapshot(info)

        if p["state"] == "absent":
            if info is not None:
                changed = True
                actions.append("removed")
                if not module.check_mode:
                    args = ["account", "rm", "--account-id", account_id]
                    if p["purge_data"]:
                        args.append("--purge-data")
                    rgw.run(*args, json_out=False)
            module.exit_json(changed=changed, actions=actions,
                             diff=diff_of(before, {"present": False}),
                             exec_mode_used=rgw.exec_mode_used)

        if info is None:
            changed = True
            actions.append("created")
            if not module.check_mode:
                args = ["account", "create", "--account-id", account_id]
                if p["name"]:
                    args += ["--account-name", p["name"]]
                rgw.run(*args, json_out=False)
                info = read_account(rgw, account_id)

        elif p["name"] and p["update_name"] and info.get("name") != p["name"]:
            changed = True
            actions.append("renamed")
            if not module.check_mode:
                rgw.run("account", "modify", "--account-id", account_id,
                        "--account-name", p["name"], json_out=False)
                info = read_account(rgw, account_id)

        wanted = p["quota"]
        if wanted is not None:
            current = snapshot(info)["quota"] if info is not None else None
            target = {"enabled": wanted["enabled"],
                      "max_size": wanted["max_size"],
                      "max_objects": wanted["max_objects"]}
            if current != target:
                changed = True
                actions.append("quota")
                if not module.check_mode:
                    rgw.run("quota", "set", "--quota-scope=account",
                            "--account-id", account_id,
                            "--max-size", target["max_size"],
                            "--max-objects", target["max_objects"], json_out=False)
                    rgw.run("quota", "enable" if target["enabled"] else "disable",
                            "--quota-scope=account", "--account-id", account_id,
                            json_out=False)
                    info = read_account(rgw, account_id)

        after = snapshot(info) if not module.check_mode or info is not None else {"present": True}
        if module.check_mode:
            if p["name"] and ("created" in actions or "renamed" in actions):
                after["name"] = p["name"]
            if "quota" in actions:
                after["quota"] = target
    except RGWError as exc:
        module.fail_json(msg="radosgw-admin failed for account %s: %s"
                             % (account_id, exc.msg),
                         rc=exc.rc, stderr=exc.stderr, cmd=exc.cmd)

    module.exit_json(changed=changed, actions=actions,
                     account={k: v for k, v in after.items() if k != "present"},
                     diff=diff_of(before, after),
                     exec_mode_used=rgw.exec_mode_used)


if __name__ == "__main__":
    main()
