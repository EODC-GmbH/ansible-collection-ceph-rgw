# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""rgw_iam_sync shows its role changes under --diff, and nothing once converged.

Run:  python3 -m unittest discover -s tests/unit
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import contextlib
import unittest
from unittest import mock

from test_rgw_oidc_replace import _bind_fqcn

_bind_fqcn()

from ansible.module_utils import basic  # noqa: E402
from module_args import patch_module_args  # noqa: E402
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_iam import compose  # noqa: E402
from test_rgw_iam import FakeRGW, live_from, make_spec  # noqa: E402

import rgw_iam_sync  # noqa: E402


class Exited(Exception):
    def __init__(self, failed, result):
        super(Exited, self).__init__(result)
        self.failed = failed
        self.result = result


def run(fake, spec, check_mode, diff):
    class StubRGW(object):
        exec_mode_used = "host"

        def __init__(self, module):
            pass

        def run(self, *args, **kwargs):
            rc, out, err = fake(*args)
            return rc, None, out, err

    def leave(failed):
        def _exit(self, **kwargs):
            raise Exited(failed, kwargs)
        return _exit

    args = dict(spec=spec, _ansible_check_mode=check_mode, _ansible_diff=diff)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch_module_args(args))
        stack.enter_context(mock.patch.object(rgw_iam_sync, "RGW", StubRGW))
        stack.enter_context(mock.patch.object(basic.AnsibleModule, "exit_json", leave(False)))
        stack.enter_context(mock.patch.object(basic.AnsibleModule, "fail_json", leave(True)))
        try:
            rgw_iam_sync.main()
        except Exited as exc:
            return exc
    raise AssertionError("module returned without exit_json")


class TestDiff(unittest.TestCase):

    def test_diff_holds_only_change_lines(self):
        spec = make_spec()
        # Without limits the converged run touches nothing but the roles.
        converged_spec = dict(spec, tenants=[dict(spec["tenants"][0], limits={})])
        converged = live_from(spec, compose(spec))
        cases = [
            # name, spec, live roles, check_mode, diff, expected prefix or None
            ("check, roles missing", spec, [], True, True, "WOULD"),
            ("apply, roles missing", spec, [], False, True, "CHANGED"),
            ("diff off", spec, [], True, False, None),
            ("converged", converged_spec, converged, True, True, None),
        ]
        for name, case_spec, roles, check_mode, diff, prefix in cases:
            with self.subTest(name):
                fake = FakeRGW(roles=list(roles), account={"max_roles": 100})
                exc = run(fake, case_spec, check_mode, diff)
                self.assertFalse(exc.failed, exc.result)
                if prefix is None:
                    self.assertNotIn("diff", exc.result)
                    continue
                lines = exc.result["diff"]["prepared"].splitlines()
                self.assertTrue(lines)
                self.assertTrue(all(line.startswith(prefix) for line in lines), lines)
                self.assertFalse(any(line.startswith("ok") for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
