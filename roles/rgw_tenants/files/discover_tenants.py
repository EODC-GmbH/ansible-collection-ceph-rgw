#!/usr/bin/env python3
"""Read-only inventory of every RGW account. Runs inside `cephadm shell`.

Emits `{"accounts": [{id, name, slug, root_user, users, roles, buckets}, ...]}` from
read-only `radosgw-admin ... list/get/info` calls only.

`bucket list` is cluster-global, so buckets are attributed by owner: the account id
(account-root created it) or one of the account's users.

Check `slug` and `root_user` by hand in any adoption stub built from this. Neither is a
field on the account, and a wrong one points the role at a phantom `$wrongslug-*` set of
roles and buckets:
 - The slug is not the account name, which carries a deploy prefix (`org-acme`). It is
   the shared prefix of the tenant's roles/buckets (`acme-admin`, `acme-data` -> `acme`),
   which role ARNs and bucket scoping key on.
 - An account can hold several `type == root` users (a break-glass admin beside the
   provisioned root), so `${slug}root` wins, then a lone root-type user.
"""
import json
import subprocess


def rgw(*args):
    out = subprocess.run(
        ["radosgw-admin", *args], capture_output=True, text=True
    ).stdout
    try:
        return json.loads(out) if out.strip() else None
    except json.JSONDecodeError:
        return None


def _derive_slug(roles, buckets):
    """Tenant slug = the common `$slug-` prefix of the account's roles/buckets.

    The slug is [a-z0-9]+ with no hyphen, so it is the token before the first
    `-`. Returned only when every hyphenated role/bucket agrees, else None for
    the operator to fill in.
    """
    prefixes = {n.split("-", 1)[0] for n in (roles + buckets) if "-" in n}
    return prefixes.pop() if len(prefixes) == 1 else None


def _pick_root_user(user_types, slug):
    """Best guess at the account-root uid this role should manage. Verify it.

    `${slug}root` wins, then a single root-type user, then the first user.
    None only for an account with no users.
    """
    uids = list(user_types)
    if slug and f"{slug}root" in user_types:
        return f"{slug}root"
    roots = [u for u, t in user_types.items() if t == "root"]
    if len(roots) == 1:
        return roots[0]
    return uids[0] if uids else None


def main():
    account_ids = rgw("account", "list") or []
    bucket_owner = {}
    for b in rgw("bucket", "list") or []:
        stats = rgw("bucket", "stats", "--bucket", b) or {}
        bucket_owner[b] = stats.get("owner", "")

    accounts = []
    for aid in account_ids:
        info = rgw("account", "get", "--account-id", aid) or {}
        users = rgw("user", "list", "--account-id", aid) or []
        # type == "root" marks an account-root user; an account may have several.
        user_types = {}
        for uid in users:
            uinfo = rgw("user", "info", "--uid", uid, "--account-id", aid) or {}
            user_types[uid] = uinfo.get("type", "")
        roles = sorted(
            n for n in (
                (r.get("RoleName") or r.get("role_name") or r.get("name") or "")
                for r in (rgw("role", "list", "--account-id", aid) or [])
            ) if n
        )
        buckets = sorted(
            b for b, owner in bucket_owner.items() if owner == aid or owner in users
        )
        slug = _derive_slug(roles, buckets)
        accounts.append(
            {
                "id": aid,
                "name": info.get("name", ""),
                "slug": slug,
                "root_user": _pick_root_user(user_types, slug),
                "users": users,
                "roles": roles,
                "buckets": buckets,
            }
        )
    print(json.dumps({"accounts": accounts}))


if __name__ == "__main__":
    main()
