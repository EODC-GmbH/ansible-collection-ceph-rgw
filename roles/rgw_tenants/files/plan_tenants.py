#!/usr/bin/env python3
"""Read-only drift preview for the configured tenants. Runs inside `cephadm shell`.

Input arrives as a file in the bind-mounted fsid dir, `/var/run/ceph/plan_input.json` =
`{"desired": $rgw_tenants_list, "tiers": [names], "default_quota": {...}}`, not as env:
`cephadm shell` does not carry the host env into the container. Read-only
`radosgw-admin ... get/list/info/stats` only.

Covers account, account-root user, storage quota, entity limits, per-tier roles and
buckets. Effective quota mirrors provision_quotas.yml: the tenant's own `quota`, else
rgw_tenants_default_quota. Don't read "up to date" as complete — OIDC provider and role
policy bodies need the account-root keys, so they are only reconciled on apply.

Emits `{"tenants": [{"name","account_id","state","present","changes":[...]}], ...}`.
"""
import json
import subprocess


def rgw(*args):
    r = subprocess.run(["radosgw-admin", *args], capture_output=True, text=True)
    try:
        return json.loads(r.stdout) if r.stdout.strip() else None
    except json.JSONDecodeError:
        return None


def _account(aid):
    return rgw("account", "get", "--account-id", aid)


def _role_name(r):
    # squid's `role list` returns AWS-style RoleName; tolerate other spellings so a schema
    # change yields no name (skipped) instead of a sorted(None) crash.
    return r.get("RoleName") or r.get("role_name") or r.get("name") or ""


def _roles(aid):
    rl = rgw("role", "list", "--account-id", aid) or []
    return sorted(n for n in (_role_name(r) for r in rl) if n)


def _bucket_exists(name):
    return rgw("bucket", "stats", "--bucket", name) is not None


def _plan_present(t, tiers, default_quota):
    """Changes a state: present tenant would incur."""
    aid, name = t["account_id"], t["name"]
    changes = []
    # provision_quotas.yml resolves `item.quota | default(rgw_tenants_default_quota, true)`:
    # an absent or empty tenant quota takes the role default, and an empty effective quota
    # leaves the account at RGW's unlimited default.
    quota = t.get("quota") or default_quota
    acct = _account(aid)
    if acct is None:
        changes.append(f"CREATE account {aid} (tenant-{name})")
        changes.append(f"CREATE account-root user {t.get('root_user') or name + 'root'}")
        for tier in tiers:
            changes.append(f"CREATE role {name}-{tier}")
        for b in t.get("buckets", []):
            changes.append(f"CREATE bucket {b}")
        if quota:
            changes.append("SET quota")
        if t.get("limits"):
            changes.append("SET limits")
        return changes

    root = t.get("root_user") or name + "root"
    if rgw("user", "info", "--uid", root, "--account-id", aid) is None:
        changes.append(f"CREATE account-root user {root}")

    if quota:
        cur = acct.get("quota", {})
        want = (str(quota.get("max_size", -1)), str(quota.get("max_objects", -1)), bool(quota.get("enabled", False)))
        have = (str(cur.get("max_size", "?")), str(cur.get("max_objects", "?")), bool(cur.get("enabled", False)))
        if want != have:
            changes.append(f"SET quota size {have[0]}->{want[0]} objs {have[1]}->{want[1]} enabled {have[2]}->{want[2]}")

    lim = t.get("limits")
    if lim is not None:
        want = (str(lim.get("max_users", -1)), str(lim.get("max_buckets", -1)))
        have = (str(acct.get("max_users", "?")), str(acct.get("max_buckets", "?")))
        if want != have:
            changes.append(f"SET limits users {have[0]}->{want[0]} buckets {have[1]}->{want[1]}")

    existing_roles = set(_roles(aid))
    for tier in tiers:
        if f"{name}-{tier}" not in existing_roles:
            changes.append(f"CREATE role {name}-{tier}")

    for b in t.get("buckets", []):
        if not _bucket_exists(b):
            changes.append(f"CREATE bucket {b}")

    return changes


def _plan_absent(t):
    """What a decommission would remove (only if it exists)."""
    aid, name = t["account_id"], t["name"]
    if _account(aid) is None:
        return []  # already gone
    changes = [f"DECOMMISSION account {aid} (tenant-{name})"]
    roles = [r for r in _roles(aid) if r.startswith(f"{name}-")]
    if roles:
        changes.append("DELETE roles " + ", ".join(roles))
    present_buckets = [b for b in t.get("buckets", []) if _bucket_exists(b)]
    if present_buckets:
        verb = "PURGE" if t.get("purge_objects") else "DELETE"
        changes.append(f"{verb} buckets " + ", ".join(present_buckets))
    return changes


_INPUT = "/var/run/ceph/plan_input.json"


def main():
    with open(_INPUT) as fh:
        cfg = json.load(fh)
    desired = cfg.get("desired", [])
    tiers = cfg.get("tiers", [])
    default_quota = cfg.get("default_quota") or {}
    out = []
    for t in desired:
        state = t.get("state", "present")
        changes = _plan_absent(t) if state == "absent" else _plan_present(t, tiers, default_quota)
        out.append({
            "name": t["name"],
            "account_id": t["account_id"],
            "state": state,
            "present": _account(t["account_id"]) is not None,
            "changes": changes,
        })
    print(json.dumps({"tenants": out}))


if __name__ == "__main__":
    main()
