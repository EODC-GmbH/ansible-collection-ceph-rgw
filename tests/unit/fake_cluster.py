# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""An in-memory Ceph cluster answering radosgw-admin and ceph argv, for converge tests.

Pure Python, so the fake executables of tests/integration/inmemory can load it too.
fake_aws.ClusterRGW puts it behind the real RGW runner.
A command the fake does not model raises,
so a module that grows a new radosgw-admin verb fails here.

Failures and output shapes follow a release profile from ceph_releases,
which test_fake_matches_capture checks against a transcript recorded on that release.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json

from ceph_releases import DEFAULT, RELEASES

READS = {("account", "list"), ("account", "get"), ("user", "list"), ("user", "info"),
         ("role", "list"), ("config", "get"), ("orch", "ps")}


def parse(argv):
    """(verb, flags) from argv in either `--flag value` or `--flag=value` form."""
    words, flags, i = [], {}, 0
    while i < len(argv):
        arg = argv[i]
        if not arg.startswith("--"):
            words.append(arg)
        elif "=" in arg:
            key, value = arg[2:].split("=", 1)
            flags[key] = value
        elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            flags[arg[2:]] = argv[i + 1]
            i += 1
        else:
            flags[arg[2:]] = True
        i += 1
    return tuple(words), flags


class Cluster(object):
    """Accounts, users, roles and config options, plus a log of every write."""

    def __init__(self, release=DEFAULT):
        self.release = release
        self.profile = RELEASES[release]
        self.accounts = {}
        self.users = {}
        self.roles = {}
        self.config = {}
        # cephadm daemons by name: {"service": ..., "started": restart count}
        self.daemons = {"rgw.rgw.ceph0.fake": {"service": "rgw.rgw", "started": 0}}
        self.writes = []

    def add_account(self, account_id, name="", quota=None, **limits):
        self.accounts[account_id] = dict(
            id=account_id, name=name,
            quota=dict(quota or {"enabled": False, "max_size": -1, "max_objects": -1}),
            **limits)

    def add_user(self, account_id, uid, keys=(), suspended=0, account_root=True,
                 display_name=None):
        self.users[(account_id, uid)] = {
            "user_id": uid, "display_name": display_name or uid, "suspended": suspended,
            "type": "root" if account_root else "rgw",
            "keys": [{"access_key": ak, "secret_key": sk} for ak, sk in keys]}

    def add_role(self, account_id, name, trust, policies=None, duration=3600):
        self.roles[(account_id, name)] = {
            "RoleName": name, "Arn": "arn:aws:iam::%s:role/%s" % (account_id, name),
            "MaxSessionDuration": duration,
            "AssumeRolePolicyDocument": json.dumps(trust),
            "PermissionPolicies": [{"PolicyName": k, "PolicyValue": json.dumps(v)}
                                   for k, v in sorted((policies or {}).items())]}

    def missing(self, verb, role=""):
        """The release's answer to verb naming an object that does not exist."""
        if verb[:2] not in self.profile["missing"]:
            raise AssertionError("fake cluster does not model radosgw-admin %s" % (verb,))
        rc, err = self.profile["missing"][verb[:2]]
        return rc, "", err.format(role=role)

    def holder(self, access_key):
        """(account_id, user) holding access_key, or None."""
        for (account, _), user in sorted(self.users.items()):
            if any(k["access_key"] == access_key for k in user["keys"]):
                return account, user
        return None

    def handle(self, argv):
        verb, f = parse(argv)
        if verb[:2] not in READS:
            self.writes.append(tuple(argv))
        if verb[:1] and verb[0] in self.profile["absent_verbs"]:
            rc, err = self.profile["unknown_verb"]
            return rc, "", err.format(verb=verb[0])
        account = f.get("account-id")
        if verb[:1] == ("config",):
            return self.config_cmd(verb, f)
        if verb[:1] == ("orch",):
            return self.orch_cmd(verb, f)
        if verb == ("account", "list"):
            return 0, json.dumps(sorted(self.accounts)), ""
        if verb == ("user", "info") and "access-key" in f:
            return self.key_holder(f["access-key"])
        if verb[:1] in (("user",), ("key",)) and verb != ("user", "list"):
            return self.user_cmd(verb, f, account)
        if verb[:1] in (("role",), ("role-policy",), ("role-trust-policy",)):
            return self.role_cmd(verb, f, account)
        if account not in self.accounts and verb != ("account", "create"):
            return self.missing(verb)
        if verb == ("user", "list"):
            return 0, json.dumps(sorted(u for a, u in self.users if a == account)), ""
        return self.account_cmd(verb, f, account)

    def config_cmd(self, verb, f):
        if verb[:2] == ("config", "get"):
            return 0, self.config.get(verb[2:4], "") + "\n", ""
        if verb[:2] == ("config", "set"):
            self.config[verb[2:4]] = verb[4]
            return 0, "", ""
        raise AssertionError("fake cluster does not model ceph %s" % (verb,))

    def orch_cmd(self, verb, f):
        """`ceph orch ps` and `ceph orch restart`. Not captured: a restart disturbs the cluster."""
        if verb[:2] == ("orch", "ps"):
            return 0, json.dumps([
                {"daemon_name": name, "service_name": d["service"], "status_desc": "running",
                 "started": "2026-10-08T00:00:%02dZ" % d["started"],
                 "container_id": "c%d" % d["started"]}
                for name, d in sorted(self.daemons.items())
                if d["service"] == f.get("service_name")]), ""
        if verb[:2] == ("orch", "restart"):
            for daemon in self.daemons.values():
                if daemon["service"] == verb[2]:
                    daemon["started"] += 1
            return 0, "Scheduled to restart %s\n" % verb[2], ""
        raise AssertionError("fake cluster does not model ceph %s" % (verb,))

    def key_holder(self, access_key):
        """`user info --access-key`, which fails differently from a missing uid."""
        held = self.holder(access_key)
        if held is None:
            rc, err = self.profile["key_nobody_holds"]
            return rc, "", err
        return 0, json.dumps(dict(held[1], account_id=held[0])), ""

    def account_cmd(self, verb, f, account):
        acct = self.accounts.get(account)
        if verb == ("account", "get"):
            return 0, json.dumps(acct), ""
        if verb == ("account", "create"):
            self.add_account(account, f.get("account-name", ""))
        elif verb == ("account", "rm"):
            del self.accounts[account]
            for key in [k for k in self.users if k[0] == account]:
                del self.users[key]
            for key in [k for k in self.roles if k[0] == account]:
                del self.roles[key]
        elif verb == ("account", "modify"):
            for key, value in f.items():
                if key == "account-name":
                    acct["name"] = value
                elif key != "account-id":
                    acct[key.replace("-", "_")] = int(value)
        elif verb == ("quota", "set"):
            acct["quota"].update(max_size=int(f["max-size"]),
                                 max_objects=int(f["max-objects"]))
        elif verb in (("quota", "enable"), ("quota", "disable")):
            acct["quota"]["enabled"] = verb[1] == "enable"
        else:
            raise AssertionError("fake cluster does not model radosgw-admin %s" % (verb,))
        return 0, "", ""

    def user_cmd(self, verb, f, account):
        key = (account, f.get("uid"))
        user = self.users.get(key)
        if verb == ("user", "create"):
            if account not in self.accounts:
                return self.missing(verb)
            pair = [(f["access-key"], f["secret-key"])] if "access-key" in f else []
            self.add_user(account, key[1], pair, account_root="account-root" in f,
                          display_name=f.get("display-name"))
            return 0, "", ""
        if user is None or account not in self.accounts:
            return self.missing(verb)
        if verb == ("user", "info"):
            return 0, json.dumps(user), ""
        if verb == ("user", "rm"):
            del self.users[key]
        elif verb in (("user", "suspend"), ("user", "enable")):
            user["suspended"] = int(verb[1] == "suspend")
        elif verb == ("key", "create"):
            user["keys"].append({"access_key": f["access-key"], "secret_key": f["secret-key"]})
        elif verb == ("key", "rm"):
            user["keys"] = [k for k in user["keys"] if k["access_key"] != f["access-key"]]
        else:
            raise AssertionError("fake cluster does not model radosgw-admin %s" % (verb,))
        return 0, "", ""

    def role_cmd(self, verb, f, account):
        key = (account, f.get("role-name"))
        if verb == ("role", "list"):
            if account not in self.accounts:
                return self.profile["roles_of_missing_account"]
            return 0, json.dumps([r for (a, _), r in sorted(self.roles.items())
                                  if a == account]), ""
        if account not in self.accounts and verb == ("role", "create"):
            return self.missing(verb)
        if verb == ("role", "create"):
            self.add_role(account, key[1], json.loads(f["assume-role-policy-doc"]),
                          duration=int(f.get("max-session-duration", 3600)))
            return 0, "", ""
        role = self.roles.get(key)
        if role is None:
            return self.missing(verb, role=key[1])
        policies = {p["PolicyName"]: p for p in role["PermissionPolicies"]}
        if verb == ("role", "delete"):
            del self.roles[key]
        elif verb == ("role", "update"):
            role["MaxSessionDuration"] = int(f["max-session-duration"])
        elif verb == ("role-trust-policy", "modify"):
            role["AssumeRolePolicyDocument"] = f["assume-role-policy-doc"]
        elif verb == ("role-policy", "put"):
            policies[f["policy-name"]] = {"PolicyName": f["policy-name"],
                                          "PolicyValue": f["policy-doc"]}
        elif verb == ("role-policy", "delete"):
            policies.pop(f["policy-name"])
        else:
            raise AssertionError("fake cluster does not model radosgw-admin %s" % (verb,))
        role["PermissionPolicies"] = [policies[k] for k in sorted(policies)]
        return 0, "", ""

