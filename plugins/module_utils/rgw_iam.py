# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""Composition of the per-tenant IAM roles a federated user assumes, and their policies.

The grant model is m:n, users x tenants x roles. A federated token carries a multivalued
claim of group paths `<grant_root>/<tenant>/<role>` and each role's trust policy requires its
own path, so one token can hold grants in several tenants and RGW picks the matching role at
assume time.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json
import re


# RGW validates role names twice: the IAM REST layer allows `_` and rejects `:`, the
# storage layer the reverse. This is the intersection (rgw_rest_iam.cc:200, rgw_role.cc:216).
ROLE_NAME_RE = re.compile(r"^[A-Za-z0-9=,.@-]+$")
MAX_ROLE_NAME_LEN = 64
USER_NAME_RE = re.compile(r"^[\w+=,.@-]+$")
MAX_USER_NAME_LEN = 64
SESSION_DURATION_MIN = 3600
SESSION_DURATION_MAX = 43200
ACCOUNT_LIMITS = ("max_users", "max_roles", "max_groups", "max_buckets", "max_access_keys")


class SpecError(Exception):
    """A tenant or catalogue declaration that must never reach RGW."""


class IAMError(Exception):
    """A radosgw-admin or IAM call that must abort the reconcile."""


def norm(doc):
    """Order-insensitive normal form, so re-serialisation is not mistaken for drift."""
    if isinstance(doc, dict):
        return {k: norm(v) for k, v in sorted(doc.items())}
    if isinstance(doc, list):
        return sorted((norm(v) for v in doc), key=lambda x: json.dumps(x, sort_keys=True))
    return doc


def same(a, b):
    return json.dumps(norm(a), sort_keys=True) == json.dumps(norm(b), sort_keys=True)


def loads(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _grants(spec, entry, label, tenant, prefix):
    """Validated capabilities, buckets and prefixes of one role or service user."""
    caps = spec["capabilities"]
    capabilities = sorted(set(entry["capabilities"]) | set(spec.get("baseline") or []))
    unknown = [c for c in capabilities if c not in caps]
    if unknown:
        raise SpecError(f"{label} names unknown capabilities {unknown}; known: {sorted(caps)}")
    buckets = entry.get("buckets") or []
    foreign = [b for b in buckets if not b.startswith(prefix)]
    if foreign:
        raise SpecError(f"{label} references buckets outside tenant '{tenant}' "
                        f"(prefix '{prefix}'): {foreign}")
    prefixes = entry.get("prefixes") or []
    if prefixes and "bucket-create" in capabilities:
        raise SpecError(f"{label} combines prefixes with bucket-create; the s3:prefix "
                        f"condition would deny the bucket creation itself")
    return {"capabilities": capabilities, "buckets": buckets or [f"{prefix}*"],
            "prefixes": prefixes}


def compose(spec):
    """Expand catalogue x tenants into concrete role specs, validating as it goes.

    Every tenant gets one role per catalogue entry plus the extra roles it declares, all
    named `<tenant>-<role>`. The tenant's own `<tenant>-<infix>-*` roles are never touched.
    """
    catalogue = spec["catalogue"]
    infix = spec["delegated_infix"]
    reserved = {entry["name"] for entry in catalogue}
    out = []

    for tenant in spec["tenants"]:
        name, account = tenant["name"], tenant["account_id"]
        if not account:
            raise SpecError(f"tenant '{name}' has no account_id")
        prefix = tenant.get("bucket_prefix") or f"{name}-"
        unknown_limits = [k for k in (tenant.get("limits") or {}) if k not in ACCOUNT_LIMITS]
        if unknown_limits:
            raise SpecError(f"tenant '{name}' sets unknown limits {unknown_limits}; "
                            f"known: {sorted(ACCOUNT_LIMITS)}")
        duration = int(tenant.get("session_duration") or SESSION_DURATION_MIN)
        if not SESSION_DURATION_MIN <= duration <= SESSION_DURATION_MAX:
            raise SpecError(f"tenant '{name}' session_duration {duration} is outside "
                            f"{SESSION_DURATION_MIN}..{SESSION_DURATION_MAX}")

        declared = [(entry, False) for entry in catalogue]
        declared += [(entry, True) for entry in tenant.get("roles") or []]
        seen = set()
        for entry, is_extra in declared:
            role = entry["name"]
            if role in seen:
                raise SpecError(f"tenant '{name}' declares role '{role}' twice")
            seen.add(role)
            if is_extra and role in reserved:
                raise SpecError(f"tenant '{name}' role '{role}' reuses a catalogue name "
                                f"{sorted(reserved)}")
            if is_extra and role.startswith(f"{infix}-"):
                raise SpecError(f"tenant '{name}' role '{role}' starts with '{infix}-', "
                                f"which is the tenant's own name space")

            full = f"{name}-{role}"
            if not ROLE_NAME_RE.match(full):
                raise SpecError(f"role name '{full}' has characters RGW rejects "
                                f"(allowed: A-Z a-z 0-9 = , . @ -)")
            if len(full) > MAX_ROLE_NAME_LEN:
                raise SpecError(f"role name '{full}' is {len(full)} characters; "
                                f"RGW's limit is {MAX_ROLE_NAME_LEN}")

            out.append(dict(_grants(spec, entry, f"role '{full}'", name, prefix),
                            name=full, tenant=name, role=role, account_id=account,
                            session_duration=duration))
    return out


def service_user_name(tenant, name):
    """The uid, display name and IAM UserName of a tenant's static-key service user.

    All three are one string: IAM resolves UserName against the display name, not the
    uid, so letting them differ makes PutUserPolicy answer NoSuchEntity.
    """
    return f"iam-{tenant}-svc-{name}"


def compose_users(spec):
    """Expand each tenant's declared service users into concrete policy targets.

    A service user gets the same capability-composed permissions a role would, minus
    delegated IAM: on a static key that is an escalation path with no session expiry.
    """
    caps = spec["capabilities"]
    out = []

    for tenant in spec["tenants"]:
        name, account = tenant["name"], tenant["account_id"]
        prefix = tenant.get("bucket_prefix") or f"{name}-"
        seen = set()

        for entry in tenant.get("users") or []:
            if entry.get("state", "present") == "absent":
                continue
            short = entry["name"]
            if short in seen:
                raise SpecError(f"tenant '{name}' declares service user '{short}' twice")
            seen.add(short)

            full = service_user_name(name, short)
            if not USER_NAME_RE.match(full):
                raise SpecError(f"service user '{full}' has characters IAM rejects "
                                f"(allowed: A-Z a-z 0-9 _ + = , . @ -)")
            if len(full) > MAX_USER_NAME_LEN:
                raise SpecError(f"service user '{full}' is {len(full)} characters; "
                                f"IAM's limit is {MAX_USER_NAME_LEN}")

            user = dict(_grants(spec, entry, f"service user '{full}'", name, prefix),
                        name=full, tenant=name, account_id=account)
            delegating = [c for c in user["capabilities"]
                          if caps[c]["scope"] == "delegated-iam"]
            if delegating:
                raise SpecError(f"service user '{full}' requests {delegating}; delegated "
                                f"IAM is for assumed roles only, never static keys")
            user["policy"] = permission_document(spec, user)
            out.append(user)
    return out


def grant_path(spec, role):
    """The group path whose membership grants this role: `<grant_root>/<tenant>/<role>`.

    The trust policy matches this string, and the IdP must hold a group at this path.
    """
    return f"{spec['grant_root']}/{role['tenant']}/{role['role']}"


def trust_document(spec, role):
    """Federate the account's OIDC provider and require the role's own grant path.

    The claim carries the full path of each group the user is in; RGW compares it verbatim.
    ForAnyValue because the claim is an array: RGW routes both forms through the same code,
    but the AWS spec distinguishes them.
    """
    account = role["account_id"]
    host = spec["provider_host"]
    return {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow",
        "Principal": {"Federated": [f"arn:aws:iam::{account}:oidc-provider/{host}"]},
        "Action": ["sts:AssumeRoleWithWebIdentity"],
        "Condition": {"ForAnyValue:StringEquals": {
            f"{host}:{spec['grant_claim']}": grant_path(spec, role)}}}]}


def permission_document(spec, role):
    """Compose the permission policy from the role's capabilities.

    Actions are grouped by the resource class their capability declares. A statement
    with no Resource never matches a request that has one (Statement::eval), so every
    scope class emits resources.
    """
    caps = spec["capabilities"]
    tenant, account = role["tenant"], role["account_id"]
    buckets, prefixes = role["buckets"], role["prefixes"]

    by_scope = {}
    for name in role["capabilities"]:
        cap = caps[name]
        by_scope.setdefault(cap["scope"], []).extend(cap["actions"])

    statements = []
    bucket_actions = sorted(set(by_scope.pop("buckets", [])))
    if bucket_actions:
        bucket_arns = [f"arn:aws:s3:::{b}" for b in buckets]
        if prefixes:
            object_arns = [f"arn:aws:s3:::{b}/{p}" for b in buckets for p in prefixes]
            # Without the condition a prefix-scoped role can still LIST the whole
            # bucket: the object statement restricts reads, not enumeration.
            statements.append({"Effect": "Allow", "Action": bucket_actions,
                               "Resource": bucket_arns,
                               "Condition": {"StringLike": {"s3:prefix": list(prefixes)}}})
            statements.append({"Effect": "Allow", "Action": bucket_actions,
                               "Resource": object_arns})
        else:
            statements.append({"Effect": "Allow", "Action": bucket_actions,
                               "Resource": bucket_arns + [f"{a}/*" for a in bucket_arns]})

    # ListAllMyBuckets is checked against arn:aws:s3::<tenant>:* (RGWListBuckets::
    # verify_permission), which only "*" reliably matches. RGW scopes the answer to the
    # calling account regardless, so this cannot reveal another tenant's buckets.
    account_actions = sorted(set(by_scope.pop("account", [])))
    if account_actions:
        statements.append({"Effect": "Allow", "Action": account_actions, "Resource": ["*"]})

    iam_actions = sorted(set(by_scope.pop("delegated-iam", [])))
    if iam_actions:
        infix = spec["delegated_infix"]
        statements.append({"Effect": "Allow", "Action": iam_actions,
                           "Resource": [f"arn:aws:iam::{account}:role/{tenant}-{infix}-*"]})

    if by_scope:
        raise SpecError(f"role '{role['name']}' uses unknown capability scope(s) "
                        f"{sorted(by_scope)}")
    if not statements:
        raise SpecError(f"role '{role['name']}' resolved to no actions at all")
    return {"Version": "2012-10-17", "Statement": statements}
