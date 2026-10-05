# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""Telling "your key does not exist" apart from "someone else owns that name".

RGW answers HeadBucket with a bare 403 for both, and HEAD carries no body, so there is no
S3 error code to read; botocore falls back to the HTTP status. Getting this wrong makes
`--check` on a greenfield tenant fail with a name-collision message about buckets that do
not exist, which is what these pin.

Run:  python3 -m unittest discover -s tests/unit
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_COLLECTION = os.path.normpath(os.path.join(_HERE, "..", ".."))
sys.path.insert(0, os.path.join(_COLLECTION, "plugins", "modules"))

from test_rgw_oidc_replace import _bind_fqcn, _stub_boto3  # noqa: E402

_stub_boto3()
_bind_fqcn()

import rgw_bucket as bucket  # noqa: E402


def client_error(code=None, status=None):
    response = {"ResponseMetadata": {"HTTPStatusCode": status}}
    if code:
        response["Error"] = {"Code": code}
    return bucket.ClientError(response, "Op")


class FakeS3(object):
    """Answers ListBuckets and HeadBucket the way RGW does."""

    def __init__(self, owned=(), list_error=None, head_errors=None):
        self.owned = list(owned)
        self.list_error = list_error
        self.head_errors = head_errors or {}
        self.heads = []

    def list_buckets(self):
        if self.list_error:
            raise self.list_error
        return {"Buckets": [{"Name": n} for n in self.owned]}

    def head_bucket(self, Bucket):
        self.heads.append(Bucket)
        error = self.head_errors.get(Bucket)
        if error:
            raise error


class TestCredentialProbe(unittest.TestCase):

    def test_a_nonexistent_key_is_a_missing_prerequisite(self):
        """ListBuckets is a GET, so its 403 does carry InvalidAccessKeyId."""
        client = FakeS3(list_error=client_error("InvalidAccessKeyId", 403))
        with self.assertRaises(bucket.ProbeError) as caught:
            bucket.owned_buckets(client)
        self.assertTrue(caught.exception.prerequisite)

    def test_a_bare_403_on_listbuckets_is_also_a_missing_prerequisite(self):
        """RGW does not always attach a code. A caller with no account cannot list."""
        client = FakeS3(list_error=client_error(None, 403))
        with self.assertRaises(bucket.ProbeError) as caught:
            bucket.owned_buckets(client)
        self.assertTrue(caught.exception.prerequisite)

    def test_an_unrelated_listbuckets_failure_is_not_a_prerequisite(self):
        client = FakeS3(list_error=client_error("ServiceUnavailable", 503))
        with self.assertRaises(bucket.ProbeError) as caught:
            bucket.owned_buckets(client)
        self.assertFalse(caught.exception.prerequisite)

    def test_a_transport_failure_is_a_probe_error_not_a_traceback(self):
        """A bad ca_bundle or an untrusted certificate surfaces as BotoCoreError."""
        client = FakeS3(list_error=bucket.BotoCoreError())
        with self.assertRaises(bucket.ProbeError) as caught:
            bucket.owned_buckets(client)
        self.assertFalse(caught.exception.prerequisite)

    def test_good_credentials_yield_the_owned_set(self):
        self.assertEqual(bucket.owned_buckets(FakeS3(owned=["a", "b"])), {"a", "b"})


class TestExistence(unittest.TestCase):

    def test_an_owned_bucket_needs_no_head_request(self):
        client = FakeS3(owned=["acme-data"])
        self.assertTrue(bucket.bucket_exists(client, "acme-data", {"acme-data"}))
        self.assertEqual(client.heads, [])

    def test_a_404_means_the_name_is_free(self):
        client = FakeS3(head_errors={"new": client_error(None, 404)})
        self.assertFalse(bucket.bucket_exists(client, "new", set()))

    def test_a_403_on_a_name_we_do_not_own_is_a_collision(self):
        """The credentials were already proven good, so 403 here is unambiguous."""
        client = FakeS3(owned=["ststest-data"],
                        head_errors={"acme-data": client_error(None, 403)})
        with self.assertRaises(bucket.ProbeError) as caught:
            bucket.bucket_exists(client, "acme-data", {"ststest-data"})
        self.assertIn("held by another account", caught.exception.msg)
        self.assertFalse(caught.exception.prerequisite)

    def test_a_head_success_for_an_unlisted_bucket_is_still_a_collision(self):
        """Reachable but not ours: it is not this account's bucket to reuse."""
        client = FakeS3(owned=[])
        with self.assertRaises(bucket.ProbeError) as caught:
            bucket.bucket_exists(client, "somebody-elses", set())
        self.assertIn("another account", caught.exception.msg)


class TestUnprobedExit(unittest.TestCase):

    def test_delete_mode_reports_absent_rather_than_changed(self):
        """Teardown revokes these keys before removing the account; a rerun must pass."""
        captured = {}

        class Module(object):
            check_mode = False

            def exit_json(self, **kwargs):
                captured.update(kwargs)
                raise SystemExit

        try:
            bucket.unprobed_exit(Module(), ["a"], "absent", "InvalidAccessKeyId")
        except SystemExit:
            pass
        self.assertFalse(captured["changed"])
        self.assertIn("gone", captured["msg"])


if __name__ == "__main__":
    unittest.main()
