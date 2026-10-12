#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: rgw_account_user
short_description: Manage a Ceph RGW account user (account-root or service) and its S3 keys
version_added: "0.3.0"
description:
  - Creates, suspends, re-enables and removes a user inside an RGW account, and optionally
    rotates its S3 key pair to a declared value.
  - The display name and account-root status are set at creation and never changed. If an
    existing user differs on either, the module fails without changing anything, in check
    mode too. See O(check_identity).
  - O(access_key) and O(secret_key) are C(no_log) individually, so a failure still shows
    radosgw-admin's output with the secret redacted.
options:
  uid:
    description: The user id. Also the default display name.
    type: str
    required: true
  account_id:
    description: The RGW account that owns the user.
    type: str
    required: true
  display_name:
    description:
      - Display name. Defaults to O(uid).
      - The user ARN is C(<account_id>:user/<display-name>) and trust policies match on it,
        so a display name that differs from the uid makes AssumeRole stop matching.
    type: str
  account_root:
    description: Create the user as an account-root user. Compared on an existing user only
      when C(user info) reports a C(type).
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
      - Move the user onto O(access_key)/O(secret_key), and re-set the secret of any
        O(keys) entry whose secret differs.
      - New keys are installed before any old key is removed, so a failed rotation leaves
        the old keys in place.
    type: bool
    default: false
  prune_other_keys:
    description:
      - With O(rotate_keys), remove every key other than O(access_key) and the O(keys)
        entries that are V(present). This runs on every rotating run, also when the user
        already holds the primary pair, so a key left over by an interrupted rotation goes.
      - Without O(rotate_keys), keys that are not declared are left alone and reported in
        RV(unmanaged_key_ids).
    type: bool
    default: true
  keys:
    description:
      - Further keys of the user besides the primary O(access_key), each with its own state.
      - A V(present) key that the user lacks is added on every run. One whose secret differs
        is reported in RV(keys_drift) and re-set only with O(rotate_keys).
      - A V(absent) key is removed on every run. Only its access key is needed.
      - Declaring a key both V(present) and V(absent), or the primary as V(absent), fails
        without changing anything.
    type: list
    elements: dict
    default: []
    suboptions:
      access_key:
        description: The access key id.
        type: str
        required: true
      secret_key:
        description: The secret key. Required when O(keys[].state=present).
        type: str
      state:
        description: Whether the user should hold this key.
        type: str
        choices: [present, absent]
        default: present
  suspended:
    description: Whether the user is suspended.
    type: bool
    default: false
  check_identity:
    description:
      - Fail when an existing user's display name or account-root status differs from
        O(display_name) and O(account_root).
      - Set false to probe the keys read-only, for example before a teardown that removes the user.
    type: bool
    default: true
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
    uid: aviatoroot
    account_id: RGW00000000000000001
    access_key: "{{ vault_rgw_tenants_aviato_access_key }}"
    secret_key: "{{ vault_rgw_tenants_aviato_secret_key }}"
    rotate_keys: true

- name: Offboard a break-glass user
  underknowledge.ceph_rgw.rgw_account_user:
    uid: iam-aviato-root-breakglass
    account_id: RGW00000000000000001
    state: absent
"""

RETURN = r"""
user:
  description: The user as it stands after the run.
  returned: when state is present, except in check mode when the user does not exist yet
  type: dict
  sample: {display_name: aviatoroot, account_root: true, suspended: false,
           access_key_ids: [BBIZ118W1T37984QJJJZ]}
keys_match:
  description:
    - Whether the user holds O(access_key) and O(secret_key) after the run, also with
      O(rotate_keys) off. When false, S3 refuses those keys as it would for a missing user.
      Null when no keys were given or the user does not exist yet.
    - In check mode a pending rotation reports false, with C(rotated) in RV(actions).
  returned: when state is present
  type: bool
  sample: true
keys_drift:
  description:
    - Access keys of V(present) O(keys) entries whose secret differs from the cluster's.
      Empty after a run with O(rotate_keys).
  returned: when state is present
  type: list
  elements: str
unmanaged_key_ids:
  description: Access keys the user holds that are neither O(access_key) nor in O(keys).
  returned: when state is present
  type: list
  elements: str
actions:
  description:
    - What the run did, or would do in check mode. Any of C(created), C(removed),
      C(rotated), C(keys_added), C(keys_removed), C(suspended), C(enabled).
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
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_read import read_user
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_user import (
    holds_pair, identity_drift, key_conflicts, plan_keys, project, snapshot, unmanaged,
)


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
        keys=dict(type="list", elements="dict", default=[], no_log=False, options=dict(
            access_key=dict(type="str", required=True, no_log=True),
            secret_key=dict(type="str", no_log=True),
            state=dict(type="str", default="present", choices=["present", "absent"]),
        ), required_if=[("state", "present", ("secret_key",))]),
        suspended=dict(type="bool", default=False),
        check_identity=dict(type="bool", default=True),
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
    primary = (p["access_key"], p["secret_key"]) if p["access_key"] else None
    keys = p["keys"]
    conflicts = key_conflicts(primary, keys)
    if conflicts:
        module.fail_json(msg="user '%s' in account %s: %s. Nothing was changed."
                             % (uid, account_id, "; ".join(conflicts)))
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

        drift = identity_drift(info, display_name, p["account_root"]) if info else []
        if drift and p["check_identity"]:
            module.fail_json(msg="user '%s' in account %s: %s. Nothing was changed; fix "
                                 "the user by hand, or correct its declaration."
                                 % (uid, account_id, "; ".join(drift)),
                             user={k: v for k, v in before.items() if k != "present"},
                             exec_mode_used=rgw.exec_mode_used)

        if info is None:
            changed = True
            actions.append("created")
            if module.check_mode:
                extra, _, _ = plan_keys(None, None, keys, False, False)
                actions += (["keys_added"] if extra else []) + (["suspended"] if p["suspended"] else [])
                after = project({"present": True, "display_name": display_name},
                                extra + ([primary] if primary else []), [], p["suspended"])
                module.exit_json(changed=True, actions=actions, keys_match=None,
                                 keys_drift=[], unmanaged_key_ids=[],
                                 diff=diff_of(before, after),
                                 exec_mode_used=rgw.exec_mode_used)
            args = ["user", "create", "--uid", uid, "--account-id", account_id,
                    "--display-name", display_name]
            if p["account_root"]:
                args.append("--account-root")
            if primary:
                args += ["--access-key", primary[0], "--secret-key", primary[1]]
            rgw.run(*args, json_out=False)
            info = read_user(rgw, uid, account_id)

        create, remove, keys_drift = plan_keys(info, primary, keys, p["rotate_keys"],
                                               p["prune_other_keys"])
        rotated = [pair for pair in create if pair == primary or pair[0] in keys_drift]
        for action, pending in (("rotated", rotated),
                                ("keys_added", [c for c in create if c not in rotated]),
                                ("keys_removed", remove)):
            if pending:
                changed = True
                actions.append(action)
        if (create or remove) and not module.check_mode:
            # Install before removing, so a failed create leaves the old keys.
            for access_key, secret_key in create:
                rgw.run("key", "create", "--uid", uid, "--account-id", account_id,
                        "--access-key", access_key, "--secret-key", secret_key,
                        json_out=False)
            for access_key in remove:
                rgw.run("key", "rm", "--uid", uid, "--account-id", account_id,
                        "--access-key", access_key, json_out=False)
            info = read_user(rgw, uid, account_id)
            keys_drift = plan_keys(info, primary, keys, False, False)[2]

        if bool(info.get("suspended", 0)) != p["suspended"]:
            changed = True
            actions.append("suspended" if p["suspended"] else "enabled")
            if not module.check_mode:
                rgw.run("user", "suspend" if p["suspended"] else "enable",
                        "--uid", uid, "--account-id", account_id, json_out=False)
                info = read_user(rgw, uid, account_id)

        if module.check_mode:
            after = project(snapshot(info), create, remove, p["suspended"])
        else:
            after = snapshot(info)
        held = {"keys": [{"access_key": a} for a in after["access_key_ids"]]}
        unmanaged_key_ids = unmanaged(held, primary, keys)
        keys_match = holds_pair(info, *primary) if primary else None
    except RGWError as exc:
        module.fail_json(msg="radosgw-admin failed for user '%s' in account %s: %s"
                             % (uid, account_id, exc.msg),
                         rc=exc.rc, stderr=exc.stderr, cmd=exc.cmd)

    module.exit_json(changed=changed, actions=actions,
                     user={k: v for k, v in after.items() if k != "present"},
                     keys_match=keys_match, keys_drift=keys_drift,
                     unmanaged_key_ids=unmanaged_key_ids,
                     diff=diff_of(before, after),
                     exec_mode_used=rgw.exec_mode_used)


if __name__ == "__main__":
    main()
