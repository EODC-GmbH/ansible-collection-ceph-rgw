# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""The exec layer: how radosgw-admin is located, invoked and redacted.

Run:  python3 -m unittest discover -s tests/unit
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                "..", "..", "plugins", "module_utils"))

from rgw import CEPH_CONF, RGW, RGWError, diff_of  # noqa: E402


class FakeModule(object):
    """Stands in for AnsibleModule: params, get_bin_path and run_command."""

    def __init__(self, params=None, bins=None, responses=None, no_log_values=()):
        self.params = {"exec_mode": "auto", "cluster": "ceph", "keyring": None,
                       "fsid": None, "image": None, "container_runtime": None}
        self.params.update(params or {})
        self.bins = bins if bins is not None else {}
        self.responses = responses or {}
        self.no_log_values = set(no_log_values)
        self.commands = []

    def get_bin_path(self, name):
        return self.bins.get(name)

    def run_command(self, argv, data=None, binary_data=False):
        self.commands.append((list(argv), data))
        for key, value in self.responses.items():
            if key in " ".join(argv):
                return value
        return 0, "", ""


class TestBinaryResolution(unittest.TestCase):

    def test_native_is_preferred_when_the_binary_is_on_path(self):
        mod = FakeModule(bins={"radosgw-admin": "/usr/bin/radosgw-admin"})
        rgw = RGW(mod)
        rgw.run("account", "list")
        self.assertEqual(rgw.exec_mode_used, "native")
        self.assertEqual(mod.commands[0][0][0], "/usr/bin/radosgw-admin")

    def test_container_is_used_when_there_is_no_host_binary(self):
        mod = FakeModule(
            bins={"docker": "/usr/bin/docker"},
            responses={"inspect": (0, "quay.io/ceph/ceph@sha256:abc\n", ""),
                       "ps": (0, "cid1\n", "")})
        rgw = RGW(mod)
        rgw.run("account", "list")
        self.assertEqual(rgw.exec_mode_used, "container")
        argv = mod.commands[-1][0]
        self.assertIn("--entrypoint", argv)
        self.assertIn("radosgw-admin", argv)
        self.assertIn("/etc/ceph:/etc/ceph:ro", argv)

    def test_cephadm_is_the_last_resort(self):
        mod = FakeModule(params={"fsid": "abc-123"}, bins={"cephadm": "/usr/sbin/cephadm"})
        rgw = RGW(mod)
        rgw.run("account", "list")
        self.assertEqual(rgw.exec_mode_used, "cephadm")
        self.assertEqual(mod.commands[-1][0][:4],
                         ["/usr/sbin/cephadm", "shell", "--fsid", "abc-123"])

    def test_no_route_to_the_cluster_is_an_error(self):
        rgw = RGW(FakeModule(bins={}))
        with self.assertRaises(RGWError):
            rgw.run("account", "list")

    def test_pinned_mode_does_not_silently_fall_back(self):
        rgw = RGW(FakeModule(params={"exec_mode": "native"}, bins={}))
        with self.assertRaises(RGWError):
            rgw.run("account", "list")


class TestGlobalArgs(unittest.TestCase):
    """cluster and keyring are documented options; they must actually reach argv."""

    def test_non_default_cluster_is_passed(self):
        mod = FakeModule(params={"cluster": "prod"},
                         bins={"radosgw-admin": "/usr/bin/radosgw-admin"})
        RGW(mod).run("account", "list")
        argv = mod.commands[-1][0]
        self.assertIn("--cluster", argv)
        self.assertEqual(argv[argv.index("--cluster") + 1], "prod")

    def test_the_default_cluster_adds_nothing(self):
        mod = FakeModule(bins={"radosgw-admin": "/usr/bin/radosgw-admin"})
        RGW(mod).run("account", "list")
        self.assertNotIn("--cluster", mod.commands[-1][0])

    def test_keyring_is_passed(self):
        mod = FakeModule(params={"keyring": "/etc/ceph/ceph.client.admin.keyring"},
                         bins={"radosgw-admin": "/usr/bin/radosgw-admin"})
        RGW(mod).run("account", "list")
        argv = mod.commands[-1][0]
        self.assertIn("--keyring", argv)
        self.assertEqual(argv[argv.index("--keyring") + 1],
                         "/etc/ceph/ceph.client.admin.keyring")

    def test_global_args_reach_the_container_path_too(self):
        mod = FakeModule(
            params={"cluster": "prod"},
            bins={"docker": "/usr/bin/docker"},
            responses={"inspect": (0, "quay.io/ceph/ceph@sha256:abc\n", ""),
                       "ps": (0, "cid1\n", "")})
        RGW(mod).run("account", "list")
        self.assertIn("--cluster", mod.commands[-1][0])


class TestStdin(unittest.TestCase):

    def test_stdin_is_closed_with_a_byte_not_an_empty_string(self):
        """run_command does `if data:` before setting stdin=PIPE.

        An empty string is falsy, so stdin would stay inherited and `cephadm shell` would
        consume the caller's.
        """
        mod = FakeModule(bins={"radosgw-admin": "/usr/bin/radosgw-admin"})
        RGW(mod).run("account", "list")
        self.assertEqual(mod.commands[-1][1], "\n")


class TestFsid(unittest.TestCase):

    def test_an_explicit_fsid_wins(self):
        self.assertEqual(RGW(FakeModule(params={"fsid": "given"})).fsid(), "given")

    @unittest.skipUnless(os.path.exists(CEPH_CONF), "no %s here" % CEPH_CONF)
    def test_fsid_comes_from_ceph_conf_without_running_anything(self):
        mod = FakeModule()
        self.assertRegex(RGW(mod).fsid(),
                         r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        self.assertEqual(mod.commands, [])


class TestOutput(unittest.TestCase):

    def test_json_is_parsed(self):
        mod = FakeModule(bins={"radosgw-admin": "/usr/bin/radosgw-admin"},
                         responses={"account": (0, '["RGW1"]', "")})
        _, parsed, _, _ = RGW(mod).run("account", "list")
        self.assertEqual(parsed, ["RGW1"])

    def test_non_json_output_parses_to_none_rather_than_raising(self):
        mod = FakeModule(bins={"radosgw-admin": "/usr/bin/radosgw-admin"},
                         responses={"account": (0, "not json", "")})
        _, parsed, out, _ = RGW(mod).run("account", "list")
        self.assertIsNone(parsed)
        self.assertEqual(out, "not json")

    def test_a_failure_raises_with_the_real_diagnostic(self):
        mod = FakeModule(bins={"radosgw-admin": "/usr/bin/radosgw-admin"},
                         responses={"user": (241, "", "duplicate key provided")})
        with self.assertRaises(RGWError) as caught:
            RGW(mod).run("user", "create")
        self.assertIn("duplicate key provided", caught.exception.msg)
        self.assertEqual(caught.exception.rc, 241)

    def test_secrets_are_redacted_from_the_diagnostic(self):
        mod = FakeModule(bins={"radosgw-admin": "/usr/bin/radosgw-admin"},
                         responses={"user": (1, "", "key SUPERSECRET rejected")},
                         no_log_values=["SUPERSECRET"])
        with self.assertRaises(RGWError) as caught:
            RGW(mod).run("user", "create")
        self.assertNotIn("SUPERSECRET", caught.exception.msg)
        self.assertIn("rejected", caught.exception.msg)

    def test_check_rc_false_returns_instead_of_raising(self):
        mod = FakeModule(bins={"radosgw-admin": "/usr/bin/radosgw-admin"},
                         responses={"user": (2, "", "nope")})
        rc, _, _, _ = RGW(mod).run("user", "info", check_rc=False)
        self.assertEqual(rc, 2)


class TestDiff(unittest.TestCase):

    def test_diff_has_the_headers_ansible_renders(self):
        diff = diff_of({"a": 1}, {"a": 2})
        self.assertEqual(diff["before"], {"a": 1})
        self.assertEqual(diff["after"], {"a": 2})
        self.assertIn("before_header", diff)
        self.assertIn("after_header", diff)


if __name__ == "__main__":
    unittest.main()
