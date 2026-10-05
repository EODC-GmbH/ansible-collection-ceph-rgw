#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: rgw_account_user
short_description: Manage a Ceph RGW account-root user and its S3 keys
version_added: "0.2.0"
description:
  - Creates, suspends, re-enables and removes a user inside an RGW account, and optionally
    rotates its S3 key pair to a declared value.
  - O(access_key) and O(secret_key) are marked C(no_log) individually rather than the task
    being C(no_log), so a failure still reports what radosgw-admin actually said with the
    secret redacted.
options:
  uid:
    description: The user id. Also used as the display name, which the STS user ARN matches on.
    type: str
    required: true
  account_id:
    description: The RGW account that owns the user.
    type: str
    required: true
  display_name:
    description:
      - Display name. Defaults to O(uid).
      - The user ARN is C(<account_id>:user/<display-name>) and trust policies match on
        it, so uid and display name must be the same string or AssumeRole silently stops
        matching.
    type: str
  account_root:
    description: Create the user as an account-root user.
    type: bool
    default: true
  access_key:
    description: S3 access key. Required with O(secret_key) when O(rotate_keys) is set.
    type: str
  secret_key:
    description: S3 secret key.
    type: str
  rotate_keys:
    description:
      - Ensure the user's key pair is exactly O(access_key)/O(secret_key).
      - The new key is installed before any old key is removed, so a failure never leaves
        the account without a usable key.
    type: bool
    default: false
  prune_other_keys:
    description: When rotating, remove every key other than O(access_key).
    type: bool
    default: true
  suspended:
    description: Whether the user is suspended.
    type: bool
    default: false
  state:
    description: Whether the user should exist.
    type: str
    choices: [present, absent]
    default: present
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
- name: Ensure the account-root user exists with the vaulted key pair
  underknowledge.ceph_rgw.rgw_account_user:
    uid: acmeroot
    account_id: RGW00000000000000001
    access_key: "{{ vault_rgw_tenants_acme_access_key }}"
    secret_key: "{{ vault_rgw_tenants_acme_secret_key }}"
    rotate_keys: true

- name: Offboard a break-glass user
  underknowledge.ceph_rgw.rgw_account_user:
    uid: iam-acme-root-breakglass
    account_id: RGW00000000000000001
    state: absent
"""

RETURN = r"""
user:
  description: The user as it stands after the run.
  returned: when state is present
  type: dict
  sample: {uid: acmeroot, suspended: false, access_key_ids: [BBIZ118W1T37984QJJJZ]}
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


def read_user(rgw, uid, account_id):
    rc, info, _, _ = rgw.run("user", "info", "--uid", uid,
                             "--account-id", account_id, check_rc=False)
    return info if rc == 0 else None


def snapshot(info):
    if info is None:
        return {"present": False}
    return {
        "present": True,
        "suspended": bool(info.get("suspended", 0)),
        "access_key_ids": sorted(k.get("access_key", "")
                                 for k in info.get("keys") or []),
    }


def main():
    argument_spec = dict(
        uid=dict(type="str", required=True),
        account_id=dict(type="str", required=True),
        display_name=dict(type="str"),
        account_root=dict(type="bool", default=True),
        access_key=dict(type="str", no_log=True),
        secret_key=dict(type="str", no_log=True),
        rotate_keys=dict(type="bool", default=False),
        prune_other_keys=dict(type="bool", default=True),
        suspended=dict(type="bool", default=False),
        state=dict(type="str", default="present", choices=["present", "absent"]),
    )
    argument_spec.update(RGW_COMMON_ARGS)

    module = AnsibleModule(
        argument_spec=argument_spec,
        required_together=[("access_key", "secret_key")],
        required_if=[("rotate_keys", True, ("access_key", "secret_key"))],
        supports_check_mode=True,
    )

    p = module.params
    uid, account_id = p["uid"], p["account_id"]
    display_name = p["display_name"] or uid
    rgw = RGW(module)
    changed = False
    actions = []

    try:
        info = read_user(rgw, uid, account_id)
        before = snapshot(info)

        if p["state"] == "absent":
            if info is not None:
                changed = True
                actions.append("removed")
                if not module.check_mode:
                    rgw.run("user", "rm", "--uid", uid, "--account-id", account_id,
                            json_out=False)
            module.exit_json(changed=changed, actions=actions,
                             diff=diff_of(before, {"present": False}),
                             exec_mode_used=rgw.exec_mode_used)

        if info is None:
            changed = True
            actions.append("created")
            if module.check_mode:
                module.exit_json(changed=True, actions=actions,
                                 diff=diff_of(before, {"present": True}),
                                 exec_mode_used=rgw.exec_mode_used)
            args = ["user", "create", "--uid", uid, "--account-id", account_id,
                    "--display-name", display_name]
            if p["account_root"]:
                args.append("--account-root")
            if p["access_key"] and p["secret_key"]:
                args += ["--access-key", p["access_key"],
                         "--secret-key", p["secret_key"]]
            rgw.run(*args, json_out=False)
            info = read_user(rgw, uid, account_id)

        elif p["rotate_keys"]:
            keys = info.get("keys") or []
            wanted = any(k.get("access_key") == p["access_key"]
                         and k.get("secret_key") == p["secret_key"] for k in keys)
            if not wanted:
                changed = True
                actions.append("rotated")
                if not module.check_mode:
                    # Install first, prune second: a failed create aborts here and the
                    # account keeps the key it already had.
                    rgw.run("key", "create", "--uid", uid, "--account-id", account_id,
                            "--access-key", p["access_key"],
                            "--secret-key", p["secret_key"], json_out=False)
                    if p["prune_other_keys"]:
                        for old in [k.get("access_key") for k in keys
                                    if k.get("access_key") != p["access_key"]]:
                            rgw.run("key", "rm", "--uid", uid,
                                    "--account-id", account_id,
                                    "--access-key", old, json_out=False)
                    info = read_user(rgw, uid, account_id)

        if info is not None and bool(info.get("suspended", 0)) != p["suspended"]:
            changed = True
            actions.append("suspended" if p["suspended"] else "enabled")
            if not module.check_mode:
                rgw.run("user", "suspend" if p["suspended"] else "enable",
                        "--uid", uid, "--account-id", account_id, json_out=False)
                info = read_user(rgw, uid, account_id)

        after = snapshot(info)
    except RGWError as exc:
        module.fail_json(msg="radosgw-admin failed for user '%s' in account %s: %s"
                             % (uid, account_id, exc.msg),
                         rc=exc.rc, stderr=exc.stderr, cmd=exc.cmd)

    module.exit_json(changed=changed, actions=actions, user=after,
                     diff=diff_of(before, after),
                     exec_mode_used=rgw.exec_mode_used)


if __name__ == "__main__":
    main()
