# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""Pure helpers for reading an existing RGW estate.

No I/O. The slug and root-user guesses are heuristics: a wrong slug makes an adoption stub
manage a phantom `<wrongslug>-*` role and bucket set, so derive_slug returns None rather
than guess.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_iam import loads


def role_entries(listing):
    """(name, entry) pairs out of `role list`, sorted, whatever shape this build returns.

    Squid returns a bare list; other builds wrap it as {"Roles": [...]}, and the name key
    has been RoleName, role_name and name across versions.
    """
    if isinstance(listing, dict):
        listing = listing.get("Roles") or []
    out = []
    for role in listing or []:
        if isinstance(role, str):
            out.append((role, {}))
            continue
        name = role.get("RoleName") or role.get("role_name") or role.get("name")
        if name:
            out.append((name, role))
    return sorted(out, key=lambda pair: pair[0])


def role_names(listing):
    return [name for name, _ in role_entries(listing)]


def inline_policies(entry):
    """{policy name: parsed document} carried in a `role list` entry."""
    return {p["PolicyName"]: loads(p.get("PolicyValue")) or {}
            for p in entry.get("PermissionPolicies") or [] if p.get("PolicyName")}


def derive_slug(roles, buckets):
    """Tenant slug = the common `<slug>-` prefix of the account's roles and buckets.

    Not the account name, which carries a deploy prefix. Returned only when every
    hyphenated name agrees, else None for the operator to fill in.
    """
    prefixes = {n.split("-", 1)[0] for n in (list(roles) + list(buckets)) if "-" in n}
    return prefixes.pop() if len(prefixes) == 1 else None


def pick_root_user(user_types, slug):
    """Best guess at the account-root uid to manage.

    An account can hold several root-type users (a break-glass admin beside the
    provisioned root), so prefer the `<slug>root` convention, then a lone root-type user,
    then the first user. None only for an account with no users.
    """
    uids = list(user_types)
    if slug and "%sroot" % slug in user_types:
        return "%sroot" % slug
    roots = [u for u, t in user_types.items() if t == "root"]
    if len(roots) == 1:
        return roots[0]
    return uids[0] if uids else None
