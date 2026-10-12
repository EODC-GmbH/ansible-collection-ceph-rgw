# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""rgw_oidc_provider state=absent when IAM refuses the account-root keys.

Run:  python3 -m unittest discover -s tests/unit
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import unittest

from test_rgw_oidc_replace import Exited, FakeModule, oidc


class ListFails(object):
    """An IAM client whose ListOpenIDConnectProviders fails with one error code."""

    def __init__(self, code):
        self.code = code

    def list_open_id_connect_providers(self):
        raise oidc.ClientError({"Error": {"Code": self.code}}, "ListOpenIDConnectProviders")


def nobody(module, code):
    return None


def aviatoroot(module, code):
    return {"user_id": "aviatoroot", "account_id": "RGW1"}


class TestAbsentOnRefusedCredentials(unittest.TestCase):
    """Squid refuses a key nobody holds with AccessDenied; radosgw-admin settles it."""

    def test_a_key_nobody_holds_means_the_account_is_gone(self):
        with self.assertRaises(Exited) as caught:
            oidc.handle_absent(FakeModule(), ListFails("AccessDenied"), "https://idp", nobody)
        self.assertFalse(caught.exception.failed)
        self.assertEqual(caught.exception.kwargs["result"], "absent")

    def test_a_refusal_of_a_held_key_is_not_reported_as_gone(self):
        """The account and its provider may still exist."""
        for code in ("AccessDenied", "SignatureDoesNotMatch"):
            with self.assertRaises(Exited) as caught:
                oidc.handle_absent(FakeModule(), ListFails(code), "https://idp", aviatoroot)
            self.assertTrue(caught.exception.failed, code)
            self.assertIn("aviatoroot", caught.exception.kwargs["msg"])

    def test_an_unrelated_code_fails_without_asking_the_cluster(self):
        def unreachable(module, code):
            raise AssertionError("radosgw-admin asked about a non-refusal")
        with self.assertRaises(Exited) as caught:
            oidc.handle_absent(FakeModule(), ListFails("ServiceUnavailable"), "https://idp",
                               unreachable)
        self.assertTrue(caught.exception.failed)


if __name__ == "__main__":
    unittest.main()
