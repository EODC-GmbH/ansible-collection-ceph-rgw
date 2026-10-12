# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""The comparable shape of an RGW account user, out of `radosgw-admin user info`."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type


def holds_pair(info, access_key, secret_key):
    """True when the user has a key whose access and secret key both match."""
    return any(k.get("access_key") == access_key and k.get("secret_key") == secret_key
               for k in info.get("keys") or [])


def snapshot(info):
    """The user as shown in the diff and RV(user). account_root only when info has a type."""
    if info is None:
        return {"present": False}
    shape = {
        "present": True,
        "display_name": info.get("display_name", ""),
        "suspended": bool(info.get("suspended", 0)),
        "access_key_ids": sorted(k.get("access_key", "")
                                 for k in info.get("keys") or []),
    }
    if "type" in info:
        shape["account_root"] = info["type"] == "root"
    return shape


def identity_drift(info, display_name, account_root):
    """Reasons an existing user differs from its declaration, or an empty list.

    Without a type in `user info`, root status is not compared.
    """
    reasons = []
    live = info.get("display_name", display_name)
    if live != display_name:
        reasons.append("display name is '%s', not '%s'. The user ARN and IAM UserName "
                       "match on the display name, so policies written for '%s' do not "
                       "apply to it" % (live, display_name, display_name))
    if "type" in info and (info["type"] == "root") != account_root:
        if account_root:
            reasons.append("is not an account-root user, but account_root is true")
        else:
            reasons.append("is an account-root user, but account_root is false")
    return reasons


def key_conflicts(primary, keys):
    """Reasons a key declaration contradicts itself, or an empty list."""
    present = {k["access_key"] for k in keys if k["state"] == "present"}
    absent = {k["access_key"] for k in keys if k["state"] == "absent"}
    reasons = []
    if present & absent:
        reasons.append("an access key is declared both present and absent")
    if primary and primary[0] in absent:
        reasons.append("the primary access_key is declared absent")
    return reasons


def plan_keys(info, primary, keys, rotate, prune):
    """The key changes a run makes: (create, remove, drift).

    create holds (access_key, secret_key) pairs, remove and drift hold access keys.
    Declared present keys are added and declared absent keys removed on every run.
    A present key whose secret differs is drift, re-set only with rotate.
    With rotate and prune, every key not declared present is removed too.
    """
    held = {k.get("access_key"): k.get("secret_key") for k in (info or {}).get("keys") or []}
    present = [(k["access_key"], k["secret_key"]) for k in keys if k["state"] == "present"]
    create, drift = [], []
    if primary and rotate and held.get(primary[0]) != primary[1]:
        create.append(primary)
    for access_key, secret_key in present:
        if access_key not in held:
            create.append((access_key, secret_key))
        elif held[access_key] != secret_key:
            drift.append(access_key)
            if rotate:
                create.append((access_key, secret_key))
    keep = {access_key for access_key, _ in present}
    if primary:
        keep.add(primary[0])
    remove = [k["access_key"] for k in keys
              if k["state"] == "absent" and k["access_key"] in held]
    if rotate and prune:
        remove += [ak for ak in held if ak not in keep and ak not in remove]
    return create, remove, drift


def unmanaged(info, primary, keys):
    """Access keys the user holds that are neither the primary nor declared."""
    declared = {k["access_key"] for k in keys}
    if primary:
        declared.add(primary[0])
    return sorted(k.get("access_key") for k in (info or {}).get("keys") or []
                  if k.get("access_key") not in declared)


def project(before, create, remove, suspended):
    """The snapshot a check-mode run would leave behind."""
    ids = set(before.get("access_key_ids", [])) - set(remove)
    ids |= {access_key for access_key, _ in create}
    return dict(before, access_key_ids=sorted(ids), suspended=suspended)
