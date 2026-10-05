#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: rgw_account_info
short_description: Read-only inventory of Ceph RGW accounts
version_added: "0.2.0"
description:
  - Reports RGW accounts, their quotas and optionally their users, roles, buckets and
    usage statistics, using only read-only C(radosgw-admin) subcommands.
options:
  account_id:
    description: Report only this account. Mutually exclusive with O(all).
    type: str
  all:
    description: Report every account on the cluster.
    type: bool
    default: false
  stats:
    description:
      - Include C(account stats) usage figures as C(stats) on each account, with C(size) in
        bytes and C(num_objects) a count.
      - Read as cached, with no C(--sync-stats) recompute, so figures are as fresh as the
        last stats sync.
    type: bool
    default: false
  buckets:
    description:
      - Attribute buckets to accounts. C(bucket list) is cluster-global, so a bucket is
        matched by owner - the account id when account-root created it, or one of the
        account's users. Costs one C(bucket stats) call per bucket.
    type: bool
    default: false
  exec_mode:
    description:
      - How to reach C(radosgw-admin). V(auto) tries the host binary, then a container
        run, then C(cephadm shell).
    type: str
    choices: [auto, native, container, cephadm]
    default: auto
  fsid:
    description: Cluster fsid. Read from C(/etc/ceph/ceph.conf) when not given.
    type: str
  image:
    description: Ceph container image. Discovered from a running ceph container when not given.
    type: str
  container_runtime:
    description: Container runtime to use. Autodetected when not given.
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
- name: Inventory every account with usage and buckets
  underknowledge.ceph_rgw.rgw_account_info:
    all: true
    stats: true
    buckets: true
  register: rgw_estate

- name: Show each quota'd account's usage against its cap
  ansible.builtin.debug:
    msg: "{{ item.id }} {{ item.stats.size }}/{{ item.quota.max_size }}"
  loop: "{{ rgw_estate.accounts | selectattr('quota.enabled') | list }}"
"""

RETURN = r"""
accounts:
  description: One entry per account reported.
  returned: always
  type: list
  elements: dict
  sample:
    - id: RGW00000000000000001
      name: acme
      slug: acme
      root_user: acmeroot
      quota: {enabled: true, max_size: 5497558138880, max_objects: 1000000}
      users: [acmeroot]
      roles: [acme-reader, acme-writer]
      buckets: [acme-data, acme-logs]
      stats: {size: 3221225472, num_objects: 1841}
exec_mode_used:
  description: Which execution path actually ran.
  returned: always
  type: str
  sample: container
"""

from ansible.module_utils.basic import AnsibleModule
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw import (
    RGW, RGW_COMMON_ARGS, RGWError,
)
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_discovery import (
    derive_slug, pick_root_user, role_names,
)


def bucket_owners(rgw):
    """bucket -> owner, resolved once. One `bucket stats` per bucket."""
    owners = {}
    _, listing, _, _ = rgw.run("bucket", "list")
    for bucket in listing or []:
        _, stats, _, _ = rgw.run("bucket", "stats", "--bucket", bucket, check_rc=False)
        owners[bucket] = (stats or {}).get("owner", "")
    return owners


def describe(rgw, account_id, want_stats, owners):
    _, info, _, _ = rgw.run("account", "get", "--account-id", account_id)
    info = info or {}
    _, users, _, _ = rgw.run("user", "list", "--account-id", account_id)
    users = users or []

    user_types = {}
    for uid in users:
        _, uinfo, _, _ = rgw.run("user", "info", "--uid", uid,
                                 "--account-id", account_id, check_rc=False)
        user_types[uid] = (uinfo or {}).get("type", "")

    _, listing, _, _ = rgw.run("role", "list", "--account-id", account_id, check_rc=False)
    roles = role_names(listing)

    buckets = sorted(b for b, owner in (owners or {}).items()
                     if owner == account_id or owner in users)
    slug = derive_slug(roles, buckets)

    quota = info.get("quota") or {}
    entry = {
        "id": account_id,
        "name": info.get("name", ""),
        "slug": slug,
        "root_user": pick_root_user(user_types, slug),
        "quota": {
            "enabled": bool(quota.get("enabled", False)),
            "max_size": quota.get("max_size", -1),
            "max_objects": quota.get("max_objects", -1),
        },
        "users": users,
        "roles": roles,
        "buckets": buckets,
    }
    if want_stats:
        _, stats, _, _ = rgw.run("account", "stats", "--account-id", account_id,
                                 check_rc=False)
        # `account stats` wraps the figures in a "stats" key beside the account id.
        entry["stats"] = (stats or {}).get("stats", {})
    return entry


def main():
    argument_spec = dict(
        account_id=dict(type="str"),
        all=dict(type="bool", default=False),
        stats=dict(type="bool", default=False),
        buckets=dict(type="bool", default=False),
    )
    argument_spec.update(RGW_COMMON_ARGS)

    module = AnsibleModule(
        argument_spec=argument_spec,
        mutually_exclusive=[("account_id", "all")],
        required_one_of=[("account_id", "all")],
        supports_check_mode=True,
    )

    rgw = RGW(module)
    try:
        if module.params["all"]:
            _, ids, _, _ = rgw.run("account", "list")
            ids = sorted(ids or [])
        else:
            ids = [module.params["account_id"]]

        owners = bucket_owners(rgw) if module.params["buckets"] else None
        accounts = [describe(rgw, aid, module.params["stats"], owners) for aid in ids]
    except RGWError as exc:
        module.fail_json(msg=exc.msg, rc=exc.rc, stderr=exc.stderr, cmd=exc.cmd)

    module.exit_json(changed=False, accounts=accounts,
                     exec_mode_used=rgw.exec_mode_used)


if __name__ == "__main__":
    main()
