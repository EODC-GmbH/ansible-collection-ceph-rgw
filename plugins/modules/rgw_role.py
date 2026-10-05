#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: rgw_role
short_description: Manage a Ceph RGW IAM role, its trust policy and its inline policies
version_added: "0.2.0"
description:
  - Creates and reconciles one IAM role inside an RGW account (trust document, inline
    permission policies, session duration), and removes it in the order RGW requires.
  - Documents are passed to C(radosgw-admin) on its argv; nothing is staged on disk.
options:
  name:
    description:
      - The role name.
      - Validated against C(^[A-Za-z0-9=,.@-]+$) and 64 characters, the intersection of
        RGW's two validators. The IAM REST layer allows C(_) and rejects C(:), the storage
        layer the reverse, so a name outside it is untouchable from one of the two.
    type: str
    required: true
  account_id:
    description: The RGW account that owns the role.
    type: str
    required: true
  assume_role_policy_document:
    description:
      - The trust policy, as a dict.
      - Required to create a role. Omit it on an existing role to leave its trust alone.
    type: dict
  policies:
    description:
      - Inline permission policies to put on the role.
      - Omit the option entirely to leave the role's inline policies alone; O(purge_policies)
        then does nothing either.
    type: list
    elements: dict
    suboptions:
      name:
        description: The inline policy name.
        type: str
        required: true
      document:
        description: The permission policy, as a dict.
        type: dict
        required: true
  max_session_duration:
    description:
      - Assumed-session lifetime in seconds, V(3600) to V(43200).
      - Omit to leave it alone; RGW defaults a new role to V(3600).
    type: int
  purge_policies:
    description:
      - Remove inline policies that O(policies) does not list.
      - Only applies when O(policies) is given, so a task that does not manage policies
        never deletes one it cannot see.
    type: bool
    default: true
  state:
    description: Whether the role should exist.
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
notes:
  - There is deliberately no C(tags) option. On this RGW build a tagged role is
    permanently unassumable (role tags become session principal tags, so every assume
    also needs C(sts:TagSession)), and C(UntagRole) is a silent no-op. A role found
    tagged is reported as a warning; the only repair is delete and recreate.
  - A trust change is applied in place with C(role-trust-policy modify). Recreating would
    mint a new RoleId and break every live session. Trust is evaluated at assume time
    only, so the change does not reach live sessions; permission policy changes do.
  - Documents are compared order-insensitively, down to the elements of C(Action) and
    C(Statement) lists, since RGW re-serialises what it stores.
  - RGW silently discards a whole inline policy that names an action it does not know.
    The role is re-read after a write and the task fails if a policy did not stick.
  - C(state=absent) deletes the inline policies and detaches the managed policies before
    C(role delete), because RGW refuses to delete a role while any policy remains.
  - Inline policies are read from the C(role list) entry. A build that reports roles
    without C(PermissionPolicies) would make O(purge_policies) see nothing to purge.
"""

EXAMPLES = r"""
- name: Reconcile a web-identity role, trust and policy both
  underknowledge.ceph_rgw.rgw_role:
    name: acme-reader
    account_id: RGW00000000000000001
    max_session_duration: 3600
    assume_role_policy_document:
      Version: "2012-10-17"
      Statement:
        - Effect: Allow
          Principal:
            Federated:
              - "arn:aws:iam::RGW00000000000000001:oidc-provider/idp.example/realms/r"
          Action: ["sts:AssumeRoleWithWebIdentity"]
          Condition:
            "ForAnyValue:StringEquals":
              "idp.example/realms/r:ceph_grants": "acme:reader"
    policies:
      - name: tenant
        document:
          Version: "2012-10-17"
          Statement:
            - Effect: Allow
              Action: ["s3:GetObject", "s3:ListBucket"]
              Resource: ["arn:aws:s3:::acme-*", "arn:aws:s3:::acme-*/*"]

- name: Leave the inline policies alone, update only the trust
  underknowledge.ceph_rgw.rgw_role:
    name: acme-reader
    account_id: RGW00000000000000001
    assume_role_policy_document: "{{ acme_trust }}"

- name: Remove a role, policies detached first
  underknowledge.ceph_rgw.rgw_role:
    name: acme-reader
    account_id: RGW00000000000000001
    state: absent
"""

RETURN = r"""
role:
  description: The role as it stands after the run.
  returned: when state is present
  type: dict
  sample:
    arn: "arn:aws:iam::RGW00000000000000001:role/acme-reader"
    max_session_duration: 3600
    policies: {}
    managed_policy_arns: []
actions:
  description: What the run did, in the order it did it.
  returned: always
  type: list
  elements: str
  sample: ["created", "policy:tenant"]
exec_mode_used:
  description: Which execution path actually ran.
  returned: always
  type: str
  sample: container
"""

import json

from ansible.module_utils.basic import AnsibleModule
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw import (
    RGW, RGW_COMMON_ARGS, RGWError, diff_of,
)
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_discovery import (
    inline_policies, role_entries,
)
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_iam import (
    MAX_ROLE_NAME_LEN, ROLE_NAME_RE, SESSION_DURATION_MAX, SESSION_DURATION_MIN,
    loads, norm, same,
)


def read_role(rgw, name, account_id):
    """The role's `role list` entry, or None.

    The listing carries the trust document, inline policies with their values, managed
    policy ARNs, session duration and tags, so nothing is left to ask per role.
    """
    rc, listing, _, _ = rgw.run("role", "list", "--account-id", account_id, check_rc=False)
    if rc != 0:
        return None
    return dict(role_entries(listing)).get(name)


def snapshot(entry):
    """The comparable state of a role, normalised so a diff shows only real drift."""
    if entry is None:
        return {"present": False}
    return {
        "present": True,
        "arn": entry.get("Arn", ""),
        "max_session_duration": int(entry.get("MaxSessionDuration") or 0),
        "assume_role_policy_document": norm(loads(entry.get("AssumeRolePolicyDocument"))
                                            or {}),
        "policies": {name: norm(doc) for name, doc in inline_policies(entry).items()},
        "managed_policy_arns": sorted(entry.get("ManagedPermissionPolicies") or []),
    }


def desired(before, trust, policies, duration, purge):
    """What the snapshot would become, for the diff of a check-mode run."""
    after = dict(before)
    after["present"] = True
    if trust is not None:
        after["assume_role_policy_document"] = norm(trust)
    if duration is not None:
        after["max_session_duration"] = duration
    current = dict(after.get("policies") or {})
    if purge:
        current = {name: doc for name, doc in current.items() if name in policies}
    current.update({name: norm(doc) for name, doc in policies.items()})
    after["policies"] = current
    return after


def detach_policies(rgw, name, account_id, entry):
    """Clear every inline and managed policy off a role; DeleteRole refuses while any remain."""
    for policy in sorted(inline_policies(entry)):
        rgw.run("role-policy", "delete", "--role-name", name,
                "--account-id", account_id, "--policy-name", policy, json_out=False)
    for arn in entry.get("ManagedPermissionPolicies") or []:
        rgw.run("role", "policy", "detach", "--role-name", name,
                "--account-id", account_id, "--policy-arn", arn, json_out=False)


def validate(module, name, duration):
    """Reject names and durations RGW would reject, with a message that says why.

    Not applied to state=absent: the storage layer permits ':', so a role made from the
    CLI can carry a name this rejects, and refusing to delete it would block `account rm`.
    """
    if not ROLE_NAME_RE.match(name):
        module.fail_json(msg="role name '%s' has characters RGW rejects (allowed: "
                             "A-Z a-z 0-9 = , . @ -). '_' passes the IAM REST layer and "
                             "fails the storage layer, ':' the reverse; only the "
                             "intersection is usable from both." % name)
    if len(name) > MAX_ROLE_NAME_LEN:
        module.fail_json(msg="role name '%s' is %d characters; RGW's limit is %d"
                             % (name, len(name), MAX_ROLE_NAME_LEN))
    if duration is not None and not (SESSION_DURATION_MIN <= duration
                                     <= SESSION_DURATION_MAX):
        module.fail_json(msg="max_session_duration %d is outside RGW's %d..%d"
                             % (duration, SESSION_DURATION_MIN, SESSION_DURATION_MAX))


def main():
    argument_spec = dict(
        name=dict(type="str", required=True),
        account_id=dict(type="str", required=True),
        assume_role_policy_document=dict(type="dict"),
        policies=dict(type="list", elements="dict", options=dict(
            name=dict(type="str", required=True),
            document=dict(type="dict", required=True),
        )),
        max_session_duration=dict(type="int"),
        purge_policies=dict(type="bool", default=True),
        state=dict(type="str", default="present", choices=["present", "absent"]),
    )
    argument_spec.update(RGW_COMMON_ARGS)

    module = AnsibleModule(argument_spec=argument_spec, supports_check_mode=True)

    p = module.params
    name, account_id = p["name"], p["account_id"]
    duration = p["max_session_duration"]
    if p["state"] == "present":
        validate(module, name, duration)

    trust = p["assume_role_policy_document"]
    wanted = {}
    for policy in p["policies"] or []:
        if policy["name"] in wanted:
            module.fail_json(msg="role '%s' declares inline policy '%s' twice"
                                 % (name, policy["name"]))
        wanted[policy["name"]] = policy["document"]
    purge = bool(p["purge_policies"]) and p["policies"] is not None

    rgw = RGW(module)
    changed = False
    actions = []

    try:
        entry = read_role(rgw, name, account_id)
        before = snapshot(entry)

        if p["state"] == "absent":
            if entry is not None:
                changed = True
                actions.append("removed")
                if not module.check_mode:
                    detach_policies(rgw, name, account_id, entry)
                    rgw.run("role", "delete", "--role-name", name,
                            "--account-id", account_id, json_out=False)
            module.exit_json(changed=changed, actions=actions,
                             diff=diff_of(before, {"present": False}),
                             exec_mode_used=rgw.exec_mode_used)

        if entry is None:
            if trust is None:
                module.fail_json(msg="role '%s' does not exist in account %s and "
                                     "assume_role_policy_document was not given; "
                                     "role create requires a trust policy"
                                     % (name, account_id))
            changed = True
            actions.append("created")
            if not module.check_mode:
                args = ["role", "create", "--role-name", name,
                        "--account-id", account_id,
                        "--assume-role-policy-doc", json.dumps(trust)]
                if duration is not None:
                    args += ["--max-session-duration", duration]
                rgw.run(*args, json_out=False)
        else:
            if entry.get("Tags"):
                module.warn("role '%s' in account %s is tagged (%s); every assume of it "
                            "fails and the tag cannot be removed. Delete and recreate it."
                            % (name, account_id,
                               ", ".join("%s=%s" % (tag.get("Key"), tag.get("Value"))
                                         for tag in entry["Tags"])))
            live_trust = loads(entry.get("AssumeRolePolicyDocument")) or {}
            if trust is not None and not same(live_trust, trust):
                changed = True
                actions.append("trust")
                if not module.check_mode:
                    rgw.run("role-trust-policy", "modify", "--role-name", name,
                            "--account-id", account_id,
                            "--assume-role-policy-doc", json.dumps(trust), json_out=False)
            if duration is not None and int(entry.get("MaxSessionDuration") or 0) != duration:
                changed = True
                actions.append("session-duration")
                if not module.check_mode:
                    rgw.run("role", "update", "--role-name", name,
                            "--account-id", account_id,
                            "--max-session-duration", duration, json_out=False)

        live_policies = inline_policies(entry)
        for policy in sorted(wanted):
            if not same(live_policies.get(policy) or {}, wanted[policy]):
                changed = True
                actions.append("policy:%s" % policy)
                if not module.check_mode:
                    rgw.run("role-policy", "put", "--role-name", name,
                            "--account-id", account_id, "--policy-name", policy,
                            "--policy-doc", json.dumps(wanted[policy]), json_out=False)
        if purge:
            for stale in sorted(set(live_policies) - set(wanted)):
                changed = True
                actions.append("purged:%s" % stale)
                if not module.check_mode:
                    rgw.run("role-policy", "delete", "--role-name", name,
                            "--account-id", account_id, "--policy-name", stale,
                            json_out=False)

        if module.check_mode:
            after = desired(before, trust, wanted, duration, purge)
        else:
            if changed:
                entry = read_role(rgw, name, account_id)
            after = snapshot(entry)
            # RGW answers a policy naming an action it does not know by discarding the
            # whole document and still exiting 0, so only a re-read shows it.
            dropped = [policy for policy in sorted(wanted)
                       if not same(after.get("policies", {}).get(policy) or {}, wanted[policy])]
            if dropped:
                module.fail_json(msg="RGW did not store inline policy %s on role '%s' as "
                                     "given; it discards a whole document that names an "
                                     "action it does not know" % (dropped, name),
                                 changed=changed, actions=actions, diff=diff_of(before, after),
                                 exec_mode_used=rgw.exec_mode_used)
    except RGWError as exc:
        module.fail_json(msg="radosgw-admin failed for role '%s' in account %s: %s"
                             % (name, account_id, exc.msg),
                         rc=exc.rc, stderr=exc.stderr, cmd=exc.cmd)

    module.exit_json(changed=changed, actions=actions,
                     role={k: v for k, v in after.items() if k != "present"},
                     diff=diff_of(before, after),
                     exec_mode_used=rgw.exec_mode_used)


if __name__ == "__main__":
    main()
