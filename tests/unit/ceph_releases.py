# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""How each Ceph release answers, for the in-memory cluster and S3/IAM fakes.

Every answer here comes from fixtures/<release>.json, recorded on a live cluster by
tests/integration/capture_rgw.sh, and test_fake_matches_capture replays that transcript
against the fakes. Failures the capture does not provoke are listed in `assumed`,
so a guess stays visible as one.

A release goes in RELEASES once it has a capture. Until then it waits in PENDING,
and the tests that run per release say so instead of running it on invented answers.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

ROLE_GONE = (2, "ERROR: failed reading role name from Role pool: {role}: "
                "(2) No such file or directory\n")
USER_NOT_POPULATED = "unable to parse request, user info was not populated\n"

SQUID = {
    "version": "19.2.4",
    # radosgw-admin and ceph: (rc, stderr) when the object a command names is missing.
    # {role} and {verb} are filled in by the fake.
    "missing": {
        ("account", "get"): (2, "ERROR: failed to read account with (2) No such file or "
                                "directory: \n"),
        ("account", "modify"): (2, "ERROR: failed to modify account with (2) No such file "
                                   "or directory: \n"),
        ("account", "rm"): (2, "ERROR: failed to remove account with (2) No such file or "
                               "directory: \n"),
        ("quota", "set"): (2, "ERROR: failed to load account\n"),
        ("quota", "enable"): (2, "ERROR: failed to load account\n"),
        ("quota", "disable"): (2, "ERROR: failed to load account\n"),
        ("user", "list"): (2, "ERROR: failed to load account\n"),
        ("user", "create"): (2, "ERROR: failed to load account\n"),
        ("user", "info"): (22, "could not fetch user info: no user info saved\n"),
        ("user", "suspend"): (2, "could not modify user: unable to modify user, "
                                 "user not found\n"),
        ("user", "enable"): (2, "could not modify user: unable to modify user, "
                                "user not found\n"),
        ("user", "rm"): (2, "could not remove user: unable to remove user, "
                            "user does not exist\n"),
        ("key", "create"): (22, "could not create key: " + USER_NOT_POPULATED),
        ("key", "rm"): (22, "could not remove key: " + USER_NOT_POPULATED),
        ("role", "create"): (2, "ERROR: failed to load account\n"),
        ("role", "delete"): ROLE_GONE,
        ("role", "update"): ROLE_GONE,
        ("role-policy", "put"): ROLE_GONE,
        ("role-policy", "delete"): ROLE_GONE,
        ("role-trust-policy", "modify"): ROLE_GONE,
    },
    # Entries of `missing` that capture.yml does not provoke.
    "assumed": {("quota", "set"), ("quota", "enable"), ("quota", "disable"),
                ("user", "create"), ("user", "enable"), ("role", "create"), ("role", "update"),
                ("role-policy", "delete"), ("role-trust-policy", "modify")},
    # `user info --access-key` for a key nobody holds.
    "key_nobody_holds": (22, "user.init failed: (22) Invalid argument\n"),
    # `role list` of a missing account succeeds with an empty list.
    "roles_of_missing_account": (0, "[]\n", ""),
    # Verbs this release's radosgw-admin does not have.
    "absent_verbs": {"oidc-provider"},
    "unknown_verb": (1, "ERROR: Unrecognized argument: '{verb}'\n"),
    # S3/IAM refusal (error code, HTTP status) by the state of the signing key.
    # HeadBucket has no body, so it answers a bare 403 for all of them.
    "refused": {"unknown key": ("AccessDenied", 403),
                "suspended user": ("UserSuspended", 403),
                "denied": ("AccessDenied", 403)},
    # A held, active key without permission for the call: not provoked by capture.yml.
    "assumed_refusals": {"denied"},
}

RELEASES = {"squid": SQUID}
PENDING = {"tentacle": "no capture yet; run tests/integration/capture_rgw.sh against a "
                       "tentacle cluster and add its profile"}
DEFAULT = "squid"
