# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""The OIDC replace path, which cannot be provoked safely against a live tenant.

RGW has no UpdateOpenIDConnectProviderThumbprint, so applying drift means deleting the
provider and creating it again. Every failure in that window leaves a tenant unable to log
in, so each one is pinned here.

Run:  python3 -m unittest discover -s tests/unit
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import os
import sys
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_COLLECTION = os.path.normpath(os.path.join(_HERE, "..", ".."))

sys.path.insert(0, os.path.join(_COLLECTION, "plugins", "modules"))


def _bind_fqcn():
    """Make `ansible_collections.underknowledge.ceph_rgw` resolve to this checkout.

    The plugins import module_utils by the collection's FQCN. Deriving the ansible_collections
    root from the path only works when the checkout sits under
    .../ansible_collections/underknowledge/ceph_rgw; binding the package chain to _COLLECTION
    works in a standalone clone too.
    """
    import importlib

    for name, path in (("ansible_collections", None),
                       ("ansible_collections.underknowledge", None),
                       ("ansible_collections.underknowledge.ceph_rgw", _COLLECTION)):
        if name in sys.modules:
            continue
        try:
            sys.modules[name] = importlib.import_module(name)
            continue
        except ImportError:
            pass
        package = types.ModuleType(name)
        package.__path__ = [path] if path else []
        sys.modules[name] = package
    # Whatever ansible_collections turned out to be, teach it where this collection lives.
    root = sys.modules["ansible_collections"]
    namespace = sys.modules["ansible_collections.underknowledge"]
    if not hasattr(root, "__path__"):
        root.__path__ = []
    if not hasattr(namespace, "__path__"):
        namespace.__path__ = []
    if _COLLECTION not in namespace.__path__:
        namespace.__path__.append(_COLLECTION)
    setattr(root, "underknowledge", namespace)


_bind_fqcn()


def _stub_boto3():
    """Make the module importable without boto3.

    boto3 lives on the Ceph host, not on every control host. The logic under test only
    talks to fake clients, so a stub keeps these tests running everywhere.
    """
    for name in ("boto3", "botocore", "botocore.config", "botocore.exceptions"):
        if name in sys.modules:
            return
    boto3 = types.ModuleType("boto3")
    boto3.client = lambda *a, **k: None
    botocore = types.ModuleType("botocore")
    config = types.ModuleType("botocore.config")
    config.Config = lambda *a, **k: None
    exceptions = types.ModuleType("botocore.exceptions")

    class BotoCoreError(Exception):
        pass

    class ClientError(Exception):
        def __init__(self, response=None, operation=None):
            super(ClientError, self).__init__(str(response))
            self.response = response or {}

    exceptions.BotoCoreError = BotoCoreError
    exceptions.ClientError = ClientError
    botocore.config = config
    botocore.exceptions = exceptions
    sys.modules.update({"boto3": boto3, "botocore": botocore,
                        "botocore.config": config, "botocore.exceptions": exceptions})


_stub_boto3()

import rgw_oidc_provider as oidc  # noqa: E402


class Exited(Exception):
    """AnsibleModule.exit_json/fail_json do not return; neither do these."""

    def __init__(self, failed, kwargs):
        super(Exited, self).__init__(kwargs.get("msg", ""))
        self.failed = failed
        self.kwargs = kwargs


class FakeModule(object):

    def __init__(self, check_mode=False):
        self.check_mode = check_mode

    def exit_json(self, **kwargs):
        raise Exited(False, kwargs)

    def fail_json(self, **kwargs):
        raise Exited(True, kwargs)


class FakeIAM(object):
    """An IAM client that fails whichever call the test names."""

    def __init__(self, fail_on=(), arn="arn:aws:iam::RGW1:oidc-provider/idp/realms/r"):
        self.fail_on = set(fail_on)
        self.arn = arn
        self.calls = []
        self.providers = [arn]

    def delete_open_id_connect_provider(self, OpenIDConnectProviderArn):
        self.calls.append(("delete", OpenIDConnectProviderArn))
        if "delete" in self.fail_on:
            # replace() catches exactly botocore errors on the delete.
            raise oidc.ClientError({"Error": {"Code": "ServiceFailure"}},
                                   "DeleteOpenIDConnectProvider")
        self.providers.remove(OpenIDConnectProviderArn)

    def create_open_id_connect_provider(self, Url, ClientIDList, ThumbprintList):
        which = "create-%d" % len([c for c in self.calls if c[0] == "create"])
        self.calls.append(("create", sorted(ThumbprintList)))
        if which in self.fail_on or "create" in self.fail_on:
            raise oidc.ClientError({"Error": {"Code": "InvalidInput"}},
                                   "CreateOpenIDConnectProvider")
        self.providers.append(self.arn)
        return {"OpenIDConnectProviderArn": self.arn}


OLD = ["a" * 40]
NEW = ["b" * 40]


def call_replace(client, module=None):
    before = oidc.snapshot(True, ["ceph-s3"], OLD)
    want = oidc.snapshot(True, ["ceph-s3"], NEW)
    return oidc.replace(module or FakeModule(), client, "https://idp/realms/r",
                        client.arn, before, want, (["ceph-s3"], OLD))


class TestReplace(unittest.TestCase):

    def test_a_clean_replace_reports_changed(self):
        client = FakeIAM()
        with self.assertRaises(Exited) as caught:
            call_replace(client)
        self.assertFalse(caught.exception.failed)
        self.assertEqual(caught.exception.kwargs["result"], "replaced")
        self.assertEqual(client.calls, [("delete", client.arn), ("create", NEW)])

    def test_a_failed_delete_leaves_the_provider_in_place(self):
        """Nothing was removed, so this must not report changed."""
        client = FakeIAM(fail_on=["delete"])
        with self.assertRaises(Exited) as caught:
            call_replace(client)
        result = caught.exception.kwargs
        self.assertTrue(caught.exception.failed)
        self.assertFalse(result["changed"])
        self.assertEqual(result["result"], "exists")
        self.assertIn("still in place", result["msg"])

    def test_a_failed_recreate_rolls_back_to_the_previous_thumbprints(self):
        client = FakeIAM(fail_on=["create-0"])
        with self.assertRaises(Exited) as caught:
            call_replace(client)
        result = caught.exception.kwargs
        self.assertTrue(caught.exception.failed)
        self.assertTrue(result["rolled_back"])
        self.assertEqual(client.calls[-1], ("create", OLD))
        self.assertEqual(client.providers, [client.arn])

    def test_a_failed_rollback_says_the_tenant_has_no_provider(self):
        """The one state a human has to repair by hand, so it must say so plainly."""
        client = FakeIAM(fail_on=["create"])
        with self.assertRaises(Exited) as caught:
            call_replace(client)
        result = caught.exception.kwargs
        self.assertTrue(caught.exception.failed)
        self.assertFalse(result["rolled_back"])
        self.assertIsNone(result["arn"])
        self.assertIn("manual intervention required", result["msg"])
        self.assertEqual(client.providers, [])


class TestThumbprintGuard(unittest.TestCase):
    """Bad input is refused before the delete, not after it."""

    def test_an_empty_list_is_refused(self):
        self.assertIn("thumbprint-less", oidc.invalid_thumbprints([]))

    def test_a_non_sha1_is_refused(self):
        self.assertIn("malformed", oidc.invalid_thumbprints(["not-a-sha1"]))

    def test_a_short_hex_string_is_refused(self):
        self.assertIn("malformed", oidc.invalid_thumbprints(["0123456789abcdef"]))

    def test_uppercase_hex_is_refused_because_rgw_stores_it_lowercased(self):
        self.assertIn("malformed", oidc.invalid_thumbprints(["A" * 40]))

    def test_a_good_thumbprint_passes(self):
        self.assertIsNone(oidc.invalid_thumbprints(["a1b2c3d4" * 5]))


if __name__ == "__main__":
    unittest.main()
