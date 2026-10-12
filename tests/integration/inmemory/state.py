# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""The in-memory cluster and its S3/IAM state, kept in one JSON file between processes.

The fake radosgw-admin and ceph executables and the fake endpoint each load it, act, and save
it, under an exclusive lock. FAKE_CEPH_STATE names the file and FAKE_CEPH_RELEASE the release it
answers as.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import contextlib
import fcntl
import json
import os
import sys

UNIT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "unit")
sys.path.insert(0, UNIT)

from fake_cluster import Cluster  # noqa: E402

# Cluster attributes keyed by tuples, which JSON has no form for.
PAIRS = ("users", "roles", "config")


class Aws(object):
    """FakeAWS's state; fake_aws.FakeAWS wraps it in the endpoint."""

    def __init__(self):
        self.buckets = set()
        self.foreign_buckets = set()
        self.providers = {}
        self.user_policies = {}


def dump(cluster, aws):
    out = {"release": cluster.release, "accounts": cluster.accounts,
           "daemons": cluster.daemons,
           "buckets": sorted(aws.buckets), "foreign_buckets": sorted(aws.foreign_buckets),
           "providers": aws.providers, "user_policies": aws.user_policies}
    for name in PAIRS:
        out[name] = [[list(key), value] for key, value in sorted(getattr(cluster, name).items())]
    return out


def load(data):
    cluster = Cluster(data["release"])
    cluster.accounts = data["accounts"]
    cluster.daemons = data["daemons"]
    for name in PAIRS:
        setattr(cluster, name, {tuple(key): value for key, value in data[name]})
    aws = Aws()
    aws.buckets = set(data["buckets"])
    aws.foreign_buckets = set(data["foreign_buckets"])
    aws.providers = data["providers"]
    aws.user_policies = data["user_policies"]
    return cluster, aws


@contextlib.contextmanager
def opened(path=None):
    """(cluster, aws) from the state file, saved back on a clean exit."""
    path = path or os.environ["FAKE_CEPH_STATE"]
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if os.path.exists(path):
            with open(path) as handle:
                cluster, aws = load(json.load(handle))
        else:
            cluster, aws = Cluster(os.environ.get("FAKE_CEPH_RELEASE", "squid")), Aws()
        yield cluster, aws
        with open(path + ".tmp", "w") as handle:
            json.dump(dump(cluster, aws), handle, indent=1, sort_keys=True)
        os.replace(path + ".tmp", path)
