# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""Nothing in the collection may still point at a deleted helper, mode or claim.

Prose and the genericized copies under examples/ drift independently of the code, so
this is checked rather than remembered.

Run:  python3 -m unittest discover -s tests/unit
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import os
import re
import unittest

_ACCOUNT_ID_LINE = re.compile(r"""account_id\s*:\s*["']?RGW""")

_COLLECTION = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

# token -> why it must not appear
RETIRED = {
    "rgw_tenants_plan": "deleted with plan.yml; --check --diff is the preview",
    "plan_tenants.py": "deleted",
    "discover_tenants.py": "replaced by the rgw_account_info module",
    "report_tenants.py": "replaced by the rgw_account_info module",
    "manage_buckets.py": "replaced by the rgw_bucket module",
    "manage_oidc_provider.py": "replaced by the rgw_oidc_provider module",
    "sync_iam.py": "replaced by the rgw_iam_sync module",
    "provision_quotas.yml": "folded into provision_accounts.yml",
    "no useful --check": "every task is a module that supports check mode",
    "rather than --check": "--check IS the plan mode now",
    "cannot introspect": "nothing runs inside a container command any more",
    "ten-minute timeout": "a converge is about 20 seconds",
}

SKIP_DIRS = {".git", "__pycache__", "tests"}
TEXT_SUFFIXES = (".yml", ".yaml", ".py", ".j2", ".md", ".cfg", ".txt")


def collection_files():
    for root, dirs, names in os.walk(_COLLECTION):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in names:
            if name.endswith(TEXT_SUFFIXES):
                yield os.path.join(root, name)


class TestNoRetiredReferences(unittest.TestCase):

    def test_nothing_references_a_deleted_helper_mode_or_claim(self):
        offenders = []
        for path in collection_files():
            with open(path) as handle:
                text = handle.read()
            for token, why in RETIRED.items():
                if token in text:
                    offenders.append("%s: '%s' (%s)"
                                     % (os.path.relpath(path, _COLLECTION), token, why))
        self.assertEqual(offenders, [], "retired references:\n  " + "\n  ".join(offenders))

    def test_no_tenant_name_example_carries_an_underscore(self):
        """RGW rejects '_' in <tenant>-<role>, so an underscored example is a trap.

        A tenant entry is recognised by its account_id, which nothing else in the
        collection carries; `name:` on its own is also roles, tasks and variables.
        """
        offenders = []
        for path in collection_files():
            with open(path) as handle:
                lines = handle.readlines()
            for number, line in enumerate(lines):
                # The literal assignment, not prose that mentions the word: a tenant
                # entry always carries account_id: "RGW<17 digits>".
                if not _ACCOUNT_ID_LINE.search(line):
                    continue
                for near in lines[max(0, number - 3):number + 4]:
                    stripped = near.strip().lstrip("#").strip()
                    if not stripped.startswith(("name:", "- name:")):
                        continue
                    value = stripped.split("name:", 1)[1].strip().strip('"\'')
                    if "_" in value and " " not in value:
                        offenders.append("%s:%d: %s"
                                         % (os.path.relpath(path, _COLLECTION),
                                            number + 1, value))
        self.assertEqual(sorted(set(offenders)), [],
                         "underscored tenant names:\n  " + "\n  ".join(sorted(set(offenders))))


if __name__ == "__main__":
    unittest.main()
