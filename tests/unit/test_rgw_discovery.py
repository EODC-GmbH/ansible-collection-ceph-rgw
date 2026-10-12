# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""policy_removals: the commands rgw_role and the IAM reconciler both run before DeleteRole.

Run:  python3 -m unittest discover -s tests/unit
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import unittest

from test_rgw_oidc_replace import _bind_fqcn

_bind_fqcn()

from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_discovery import (  # noqa: E402
    policy_removals,
)

ACCOUNT = "RGW00000000000000001"
ARN = "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess"


class TestPolicyRemovals(unittest.TestCase):

    def test_inline_policies_sorted_then_managed_detached(self):
        entry = {
            "PermissionPolicies": [{"PolicyName": "tenant", "PolicyValue": "{}"},
                                   {"PolicyName": "extra", "PolicyValue": "{}"}],
            "ManagedPermissionPolicies": [ARN],
        }
        self.assertEqual(
            [args for _what, args in policy_removals("aviato-reader", ACCOUNT, entry)],
            [("role-policy", "delete", "--role-name=aviato-reader",
              "--account-id=" + ACCOUNT, "--policy-name=extra"),
             ("role-policy", "delete", "--role-name=aviato-reader",
              "--account-id=" + ACCOUNT, "--policy-name=tenant"),
             ("role", "policy", "detach", "--role-name=aviato-reader",
              "--account-id=" + ACCOUNT, "--policy-arn=" + ARN)])

    def test_a_bare_role_needs_nothing(self):
        self.assertEqual(policy_removals("aviato-reader", ACCOUNT, {}), [])

    def test_a_nameless_inline_entry_is_skipped(self):
        entry = {"PermissionPolicies": [{"PolicyValue": "{}"}]}
        self.assertEqual(policy_removals("aviato-reader", ACCOUNT, entry), [])


if __name__ == "__main__":
    unittest.main()
