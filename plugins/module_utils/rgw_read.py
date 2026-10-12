# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""radosgw-admin reads that tell a missing object from a failed call.

radosgw-admin exits non-zero both for a missing user, account or role
and for a cluster it cannot reach.
A failed read counts as absent only when a listing confirms it;
otherwise the read is repeated with check_rc so its error surfaces.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw import RGWError


def listing(rgw, *args):
    """A radosgw-admin listing: a JSON array, or no output for an empty one."""
    _, parsed, out, _ = rgw.run(*args)
    if not (out or "").strip():
        return []
    if not isinstance(parsed, list):
        raise RGWError("`radosgw-admin %s` did not print a JSON array (%d bytes of output)"
                       % (" ".join(args[:2]), len(out or "")))
    return parsed


def account_listed(rgw, account_id):
    return account_id in listing(rgw, "account", "list")


def read(rgw, absent, *args, **kwargs):
    """The parsed JSON of a read, or None when absent() confirms there is nothing.

    absent() runs only after the read fails.
    A successful read with no output returns `empty` when given, else raises.
    Errors report the output length only, because `user info` prints the user's secret keys.
    """
    rc, parsed, out, _ = rgw.run(*args, check_rc=False)
    if rc != 0:
        if absent():
            return None
        rc, parsed, out, _ = rgw.run(*args)
    if parsed is None and "empty" in kwargs and not (out or "").strip():
        return kwargs["empty"]
    if parsed is None:
        raise RGWError("`radosgw-admin %s` printed no JSON (%d bytes of output)"
                       % (" ".join(args[:2]), len(out or "")), rc=rc)
    return parsed


def read_user(rgw, uid, account_id):
    def absent():
        return (not account_listed(rgw, account_id)
                or uid not in listing(rgw, "user", "list", "--account-id", account_id))
    return read(rgw, absent, "user", "info", "--uid", uid, "--account-id", account_id)


def read_account(rgw, account_id):
    return read(rgw, lambda: not account_listed(rgw, account_id),
                "account", "get", "--account-id", account_id)


def read_roles(rgw, account_id):
    """The account's `role list`, or None when the account does not exist."""
    return read(rgw, lambda: not account_listed(rgw, account_id),
                "role", "list", "--account-id", account_id, empty=[])


def read_key_holder(rgw, access_key):
    """The `user info` of the user holding access_key, or None when no user holds it.

    Squid exits 22 (EINVAL) for a key nobody holds and for other bad input,
    so a failed lookup counts as absent only once `account list` shows the cluster answers.
    """
    def absent():
        listing(rgw, "account", "list")
        return True
    return read(rgw, absent, "user", "info", "--access-key", access_key)
