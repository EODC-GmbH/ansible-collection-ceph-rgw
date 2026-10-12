# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""The fakes answer as the release they claim to be, by its recorded transcript.

fixtures/<release>.json is written by tests/integration/capture_rgw.sh on a live cluster. Its
steps are replayed in order against a fresh Cluster and FakeAWS of that release:
  - radosgw-admin and ceph: the same exit code; on failure the same first stderr line,
    log prefix aside; for a read, JSON whose keys and types the real output has too
    (the fake may leave fields out, but not invent them or change their type).
  - S3 and IAM: the same error code and HTTP status.
Writes are compared by exit code only: the modules do not read what they print.

Run:  python3 -m unittest discover -s tests/unit
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import glob
import json
import os
import re
import unittest

from ceph_releases import PENDING, RELEASES
from fake_aws import FakeAWS
from fake_cluster import READS, Cluster, parse

from botocore.exceptions import ClientError  # noqa: E402  (fake_aws stubs it if missing)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
# `2026-10-08T10:05:05.456+0000 7fbd2c70a900  0 ERROR: ...`
LOG_PREFIX = re.compile(r"^\d{4}-\d\d-\d\dT\S+ [0-9a-f]+ +-?\d+ ")


def captures():
    for path in sorted(glob.glob(os.path.join(FIXTURES, "*.json"))):
        with open(path) as handle:
            yield json.load(handle)


def first_line(text):
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return LOG_PREFIX.sub("", lines[0]) if lines else ""


def shape(value):
    if isinstance(value, dict):
        return {k: shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    return type(value).__name__


def misfits(fake, real, path="$"):
    """Where the fake's shape is not part of the real one."""
    if isinstance(fake, dict) and isinstance(real, dict):
        out = []
        for key, value in fake.items():
            if key not in real:
                out.append("%s.%s: not in the real output" % (path, key))
            else:
                out += misfits(value, real[key], "%s.%s" % (path, key))
        return out
    if isinstance(fake, list) and isinstance(real, list):
        return misfits(fake[0], real[0], path + "[0]") if fake and real else []
    return [] if fake == real else ["%s: %s here, %s on the cluster" % (path, fake, real)]


def aws_answer(aws, step):
    client = aws.client(step["via"], aws_access_key_id=step["access_key"])
    try:
        getattr(client, step["call"])(**step["params"])
    except ClientError as exc:
        response = exc.response
        return response["Error"]["Code"], response["ResponseMetadata"]["HTTPStatusCode"]
    return "", 200


class FakeMatchesCapture(unittest.TestCase):

    def check_cli(self, cluster, step):
        rc, out, err = cluster.handle(step["argv"])
        self.assertEqual(rc, step["rc"], "exit code")
        if rc != 0:
            self.assertEqual(first_line(err), first_line(step["stderr"]), "stderr")
            return
        verb = parse(step["argv"])[0]
        if verb[:2] in READS and step["stdout"].lstrip()[:1] in ("{", "["):
            self.assertEqual(misfits(shape(json.loads(out)),
                                     shape(json.loads(step["stdout"]))), [])

    def test_replay(self):
        for capture in captures():
            release = capture["release"]
            if release not in RELEASES:
                continue
            cluster = Cluster(release)
            aws = FakeAWS(cluster)
            for step in capture["steps"]:
                with self.subTest(release=release, step=step["name"]):
                    if "argv" in step:
                        self.check_cli(cluster, step)
                    else:
                        self.assertEqual(aws_answer(aws, step),
                                         (step["code"], step["status"]))

    def test_every_profile_answer_is_captured_or_marked_assumed(self):
        for capture in captures():
            profile = RELEASES.get(capture["release"])
            if profile is None:
                continue
            failed = {parse(s["argv"])[0][:2] for s in capture["steps"]
                      if "argv" in s and s["rc"] != 0}
            unproven = set(profile["missing"]) - profile["assumed"] - failed
            self.assertEqual(unproven, set(), "%s: answers neither captured nor marked "
                                              "assumed" % capture["release"])

    def test_every_release_has_a_capture(self):
        captured = {capture["release"] for capture in captures()}
        self.assertEqual(set(RELEASES) - captured, set())

    def test_pending_releases(self):
        for release, reason in sorted(PENDING.items()):
            with self.subTest(release=release):
                self.skipTest("%s: %s" % (release, reason))


if __name__ == "__main__":
    unittest.main()
