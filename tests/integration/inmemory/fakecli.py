# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""radosgw-admin or ceph, answered by the in-memory cluster in FAKE_CEPH_STATE.

    python3 fakecli.py <radosgw-admin|ceph> <args...>

bin/ holds the two executables that call this. A command the fake does not model exits 70 and
says so on stderr.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import sys

from state import opened

# Global options the collection's runner may add; the fake has one cluster.
GLOBAL = ("--cluster", "--keyring")


def strip_globals(argv):
    out, skip = [], False
    for arg in argv:
        if skip:
            skip = False
        elif arg in GLOBAL:
            skip = True
        else:
            out.append(arg)
    return out


def main(argv):
    binary, args = argv[0], strip_globals(argv[1:])
    try:
        with opened() as (cluster, _):
            rc, out, err = cluster.handle(args)
    except AssertionError as exc:
        sys.stderr.write("fake %s: %s\n" % (binary, exc))
        return 70
    sys.stdout.write(out)
    sys.stderr.write(err)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
