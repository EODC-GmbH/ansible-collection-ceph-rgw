# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""Reconciles the composed IAM roles against a live RGW.

radosgw-admin is injected, so this is unit-testable without a cluster.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json

from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_iam import (
    IAMError, loads, permission_document, same, trust_document,
)


class Runner:
    """radosgw-admin, injected as run(*args) -> (rc, stdout, stderr)."""

    def __init__(self, run):
        self._run = run

    def __call__(self, *args):
        return self._run(*args)

    def must(self, what, *args):
        """Run, and abort unless RGW accepted it.

        RGW answers a policy naming an action it does not know by discarding the whole
        document and still exiting 0, leaving the role with no permissions at all. Never
        soften these output checks to a warning.
        """
        rc, out, err = self._run(*args)
        blob = out + err
        if rc != 0 or "failed to parse" in blob or "is not a valid action" in blob:
            raise IAMError("%s: rc=%s %s" % (what, rc, blob.strip()[:500]))
        return out


def drop_policies(rgw, name, account, live):
    """DeleteRole refuses while ANY inline or managed policy remains, so clear both."""
    for policy in live.get("PermissionPolicies") or []:
        rgw.must(f"delete policy {policy['PolicyName']} on {name}", "role-policy", "delete",
                 f"--role-name={name}", f"--account-id={account}",
                 f"--policy-name={policy['PolicyName']}")
    for arn in live.get("ManagedPermissionPolicies") or []:
        rgw.must(f"detach {arn} from {name}", "role", "policy", "detach",
                 f"--role-name={name}", f"--account-id={account}", f"--policy-arn={arn}")


def tag_pairs(role):
    return ", ".join(f"{t.get('Key')}={t.get('Value')}" for t in role["Tags"])


class Reconciler:
    def __init__(self, spec, rgw, dry_run=False):
        self.spec = spec
        self.rgw = rgw
        self.dry_run = dry_run
        self.actions = []
        self.policy_name = spec.get("policy_name") or "tenant"
        self.changed = False
        self.touched = False

    def act(self, line, what, *args, _pre=None):
        """Report a change, and make it unless this is a dry run."""
        self.changed = True
        self.touched = True
        self.actions.append(f"{'WOULD  ' if self.dry_run else 'CHANGED'} {line}")
        if not self.dry_run:
            if _pre is not None:
                _pre()
            self.rgw.must(what, *args)

    def live_roles(self, account):
        """Every role in the account, by name. radosgw-admin pages internally."""
        rc, out, err = self.rgw("role", "list", f"--account-id={account}")
        if rc != 0:
            raise IAMError(f"role list {account}: {err.strip()[:300]}")
        return {r["RoleName"]: r for r in (loads(out) or [])}

    def create(self, line, name, account, trust, duration, _pre=None):
        self.act(line, f"create role {name}", "role", "create", f"--role-name={name}",
                 f"--account-id={account}", f"--assume-role-policy-doc={json.dumps(trust)}",
                 f"--max-session-duration={duration}", _pre=_pre)

    def role(self, want, live):
        """Bring one platform role to its declared trust, policy and session duration."""
        name, account = want["name"], want["account_id"]
        duration = want["session_duration"]
        self.touched = False
        trust = trust_document(self.spec, want)
        perm = permission_document(self.spec, want)
        current = live.get(name)
        if current and current.get("Tags"):
            if self.spec.get("prune"):
                self.rebuild_tagged(want, current, trust, perm)
                return
            self.actions.append(f"BROKEN  {name} ({account}): tagged out of band "
                                f"({tag_pairs(current)}); every assume of this role fails. "
                                f"The tag cannot be removed; set rgw_iam_prune=true to "
                                f"rebuild the role.")

        if current is None:
            self.create(f"{name} ({account}): created", name, account, trust, duration)
            current = {}
        else:
            if not same(loads(current.get("AssumeRolePolicyDocument")) or {}, trust):
                # In place: recreating the role would mint a new RoleId, invalidate every
                # live session and drop the permission policies.
                self.act(f"{name} ({account}): trust policy updated",
                         f"modify trust of {name}", "role-trust-policy", "modify",
                         f"--role-name={name}", f"--account-id={account}",
                         f"--assume-role-policy-doc={json.dumps(trust)}")
            if int(current.get("MaxSessionDuration") or 0) != duration:
                self.act(f"{name} ({account}): session duration -> {duration}s",
                         f"update duration of {name}", "role", "update",
                         f"--role-name={name}", f"--account-id={account}",
                         f"--max-session-duration={duration}")

        policies = {p["PolicyName"]: loads(p["PolicyValue"])
                    for p in current.get("PermissionPolicies") or []}
        if not same(policies.get(self.policy_name) or {}, perm):
            self.act(f"{name} ({account}): permission policy applied "
                     f"[{', '.join(want['capabilities'])}]",
                     f"put policy on {name}", "role-policy", "put",
                     f"--role-name={name}", f"--account-id={account}",
                     f"--policy-name={self.policy_name}", f"--policy-doc={json.dumps(perm)}")
        if not self.touched:
            self.actions.append(f"ok      {name} ({account})")

        # An inline policy this reconciler did not put, or a managed policy attached out
        # of band, is extra authority. Always reported; removed only under prune.
        for stale in sorted(set(policies) - {self.policy_name}):
            if self.spec.get("prune"):
                self.act(f"{name} ({account}): stale inline policy '{stale}' removed",
                         f"delete policy {stale} on {name}", "role-policy", "delete",
                         f"--role-name={name}", f"--account-id={account}",
                         f"--policy-name={stale}")
            else:
                self.actions.append(f"EXTRA   {name} ({account}): inline policy '{stale}' "
                                    f"was not put here; set rgw_iam_prune=true to remove it")
        for arn in current.get("ManagedPermissionPolicies") or []:
            self.actions.append(f"EXTRA   {name} ({account}): managed policy {arn} "
                                f"attached out of band")

    def rebuild_tagged(self, want, current, trust, perm):
        """A tagged platform role is unassumable, and the tag cannot be taken off.

        Role tags become the session's principal tags, so the assume handler also demands
        sts:TagSession from the trust policy, which nothing written here carries.
        radosgw-admin has no tag verb and IAM UntagRole is a silent no-op on this build,
        so delete-and-recreate is the only repair. It is safe because this reconciler owns
        the name and rebuilds it identically.
        """
        name, account = want["name"], want["account_id"]

        def delete():
            drop_policies(self.rgw, name, account, current)
            self.rgw.must(f"delete role {name}", "role", "delete",
                          f"--role-name={name}", f"--account-id={account}")

        self.create(f"{name} ({account}): rebuilt, was tagged out of band "
                    f"({tag_pairs(current)})",
                    name, account, trust, want["session_duration"], _pre=delete)
        if not self.dry_run:
            self.rgw.must(f"put policy on {name}", "role-policy", "put",
                          f"--role-name={name}", f"--account-id={account}",
                          f"--policy-name={self.policy_name}",
                          f"--policy-doc={json.dumps(perm)}")

    def prune(self, tenant, account, keep, live):
        """Remove platform-namespace roles the spec no longer declares.

        The tenant's own `<tenant>-<infix>-*` roles, and anything without the tenant's
        prefix, are never touched.
        """
        infix = self.spec["delegated_infix"]
        for name in sorted(live):
            if not name.startswith(f"{tenant}-") or name in keep:
                continue
            if name.startswith(f"{tenant}-{infix}-"):
                continue
            if not self.spec.get("prune"):
                self.actions.append(f"STALE   {name} ({account}): in the platform name space "
                                    f"but not in the spec; set rgw_iam_prune=true to remove it")
                continue
            self.act(f"{name} ({account}): stale platform role pruned",
                     f"delete role {name}", "role", "delete",
                     f"--role-name={name}", f"--account-id={account}",
                     _pre=lambda: drop_policies(self.rgw, name, account, live[name]))

    def limits(self, tenant, account, wanted):
        """Cap how many roles, users, groups and keys the account may hold.

        RGW's defaults are 1000/1000/1000/1000/4 (RGWAccountInfo), which is the ceiling a
        tenant holding iam-delegate can mint up to.
        """
        if not wanted:
            return
        rc, out, _ = self.rgw("account", "get", f"--account-id={account}")
        current = loads(out) if rc == 0 else None
        if current is None:
            self.actions.append(f"SKIP    {tenant} ({account}): account get failed, "
                                f"limits not reconciled")
            return
        drift = {k: v for k, v in wanted.items() if int(current.get(k, -1)) != int(v)}
        if not drift:
            return
        args = [f"--{k.replace('_', '-')}={v}" for k, v in sorted(drift.items())]
        self.act(f"{tenant} ({account}): account limits {drift}",
                 f"modify account {account}", "account", "modify",
                 f"--account-id={account}", *args)

    def run(self, roles):
        by_account = {}
        for want in roles:
            by_account.setdefault((want["tenant"], want["account_id"]), []).append(want)

        for (tenant, account), wants in sorted(by_account.items()):
            live = self.live_roles(account)
            for want in wants:
                self.role(want, live)
            self.prune(tenant, account, {w["name"] for w in wants}, live)

        for tenant in self.spec["tenants"]:
            self.limits(tenant["name"], tenant["account_id"], tenant.get("limits") or {})
