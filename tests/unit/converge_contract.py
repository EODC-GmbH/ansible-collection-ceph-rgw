# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""The converge-twice contract, for one module or for steps run in a role's order.

The steps run four times against one in-memory cluster, as converge_twice.sh runs the roles
against a live one:
  1. --check   succeeds and writes nothing.
  2. real      succeeds. For a single step, changes if and only if --check said it would.
  3. real      reports no change and writes nothing.
  4. --check   reports no change.
In every run, a step that reports a change shows it in its diff.

With several steps, --check of a later step cannot see what an earlier one would do, so step 1
is held to succeeding without writes, not to predicting step 2.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import importlib
import unittest

from fake_aws import ACCOUNT, FakeAWS, run_module
from fake_cluster import Cluster

VAULT = ("AKVAULT", "SKVAULT")
ROOT = "aviatoroot"


def fresh(seed, release):
    """A cluster of `release` and its S3/IAM, seeded, with the seeding writes cleared."""
    cluster = Cluster(release)
    aws = FakeAWS(cluster)
    seed(cluster, aws)
    del cluster.writes[:], aws.writes[:]
    return cluster, aws


def writes(cluster, aws):
    return cluster.writes + aws.writes


def no_seed(cluster, aws):
    pass


def account(cluster, aws):
    cluster.add_account(ACCOUNT, "tenant-aviato")


def root(cluster, aws):
    """The account and its root user holding the vaulted keys, as S3/IAM steps need."""
    account(cluster, aws)
    cluster.add_user(ACCOUNT, ROOT, [VAULT])


def seeded(*steps):
    def seed(cluster, aws):
        for step in steps:
            step(cluster, aws)
    return seed


class ConvergeCase(unittest.TestCase):

    def run_step(self, label, name, params, cluster, aws, check_mode):
        res = run_module(importlib.import_module(name), params, cluster, aws,
                         check_mode=check_mode)
        mode = "--check" if check_mode else "real run"
        self.assertFalse(res.failed, "%s, %s %s: %s"
                         % (label, mode, name, res.kwargs.get("msg")))
        self.assert_diff_shows_change(res.kwargs, "%s, %s %s" % (label, mode, name))
        return res.kwargs

    def assert_diff_shows_change(self, result, where):
        diff = result.get("diff")
        if not result.get("changed") or not isinstance(diff, dict) or "before" not in diff:
            return
        self.assertNotEqual(diff["before"], diff.get("after"),
                            "%s: changed=true, but the diff shows no change" % where)

    def run_all(self, label, steps, cluster, aws, check_mode=False):
        return [self.run_step(label, name, params, cluster, aws, check_mode)["changed"]
                for name, params in steps]

    def converge(self, label, steps, seed, release, first_changes=None):
        """Run the four steps. first_changes, for a single step, is what --check must say."""
        cluster, aws = fresh(seed, release)
        check = self.run_all(label, steps, cluster, aws, check_mode=True)
        self.assertEqual(writes(cluster, aws), [], "%s: --check wrote" % label)
        if first_changes is not None:
            self.assertEqual(check, [first_changes], "%s: --check" % label)

        first = self.run_all(label, steps, cluster, aws)
        if len(steps) == 1:
            self.assertEqual(first, check, "%s: --check mispredicted the real run" % label)

        del cluster.writes[:], aws.writes[:]
        second = self.run_all(label, steps, cluster, aws)
        self.assertEqual(second, [False] * len(steps), "%s: second run changed" % label)
        self.assertEqual(writes(cluster, aws), [], "%s: second run wrote" % label)

        after = self.run_all(label, steps, cluster, aws, check_mode=True)
        self.assertEqual(after, [False] * len(steps),
                         "%s: --check after converging reports a change" % label)
        return cluster, aws
