#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: rgw_iam_sync
short_description: Reconcile the per-tenant IAM roles a federated user assumes
version_added: "0.3.0"
description:
  - Composes the platform IAM roles for every tenant in O(spec), reads the account's live
    roles in one pass, and converges trust policies, permission policies, session
    durations and account entity limits.
  - The grant model is m:n. A federated token carries a multivalued claim of group paths
    C(<grant_root>/<tenant>/<role>) and each role's trust policy requires its own path, so
    one token can hold grants in several tenants.
  - Under C(--check) nothing is written and every action is reported as C(WOULD).
options:
  spec:
    description:
      - The desired-state document with the capability catalogue, role catalogue and the
        tenants to reconcile. Built by the C(rgw_iam) role.
    type: dict
    required: true
  prune:
    description:
      - Remove platform roles and inline policies that O(spec) no longer declares, and
        rebuild a role that was tagged out of band.
      - Without it these are reported as V(STALE), V(EXTRA) and V(BROKEN) and left alone.
      - A tenant's own C(<tenant>-<infix>-*) roles are never pruned.
    type: bool
    default: false
  exec_mode:
    description:
      - How to reach C(radosgw-admin). V(auto) tries the host binary, then a container
        run, then C(cephadm shell).
    type: str
    choices: [auto, native, container, cephadm]
    default: auto
  fsid:
    description: Cluster fsid. Read from C(/etc/ceph/ceph.conf) when not given.
    type: str
  image:
    description: Ceph container image. Discovered from a running ceph container when not given.
    type: str
  container_runtime:
    description: Container runtime to use. Autodetected when not given.
    type: str
    choices: [docker, podman]
  cluster:
    description: Ceph cluster name.
    type: str
    default: ceph
  keyring:
    description: Path to a ceph keyring.
    type: str
notes:
  - RGW answers a policy containing an action it does not recognise by discarding the
    whole document and still exiting 0, leaving the role with no permissions. This module
    treats that output as fatal.
  - A role tagged out of band cannot be assumed. Role tags become the session's principal
    tags, so the assume handler also demands C(sts:TagSession), which no trust policy
    written here carries. C(UntagRole) is a no-op on this build, so O(prune) deletes and
    recreates the role.
  - Trust policies are updated in place. Recreating a role mints a new RoleId, which
    invalidates every live session and drops the permission policies.
author:
  - Underknowledge (@Underknowledge)
"""

EXAMPLES = r"""
- name: Reconcile IAM roles, policies and account limits
  underknowledge.ceph_rgw.rgw_iam_sync:
    spec: "{{ rgw_iam_spec }}"
    prune: "{{ rgw_iam_prune }}"
  register: iam

- name: Show what the reconciler did
  ansible.builtin.debug:
    var: iam.actions

- name: Preview without writing anything
  underknowledge.ceph_rgw.rgw_iam_sync:
    spec: "{{ rgw_iam_spec }}"
  check_mode: true
"""

RETURN = r"""
actions:
  description:
    - One line per role, prefixed V(ok), V(CHANGED), V(WOULD), V(STALE), V(EXTRA),
      V(BROKEN) or V(SKIP).
  returned: always
  type: list
  elements: str
  sample:
    - "ok      aviato-reader (RGW00000000000000001)"
    - "CHANGED aviato-writer (RGW00000000000000001): permission policy applied [objects-write]"
roles:
  description: Names of the roles the spec composed to.
  returned: always
  type: list
  elements: str
required_groups:
  description:
    - The group paths the IdP must contain for these roles to be reachable, one
      C(<grant_root>/<tenant>/<role>) per composed role. The collection does not create
      these groups.
  returned: always
  type: list
  elements: str
  sample:
    - "/services/s3/aviato/reader"
    - "/services/s3/aviato/writer"
diff:
  description:
    - With C(--diff), the V(CHANGED), V(WOULD) and V(SKIP) lines of RV(actions) as one
      C(prepared) text. Left out when there are none.
  returned: when diff mode is on and the run changed or would change something
  type: dict
exec_mode_used:
  description: Which execution path actually ran.
  returned: always
  type: str
  sample: container
"""

from ansible.module_utils.basic import AnsibleModule
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw import (
    RGW, RGW_COMMON_ARGS, RGWError,
)
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_iam import (
    IAMError, SpecError, compose, grant_path,
)
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_iam_reconcile import (
    Reconciler, Runner,
)

# Action prefixes that change the cluster, or would under --check.
CHANGE_PREFIXES = ("CHANGED", "WOULD", "SKIP")


def changeset(module, actions):
    """The --diff of a run: its change lines only, so a converged run prints none."""
    lines = [line for line in actions if line.startswith(CHANGE_PREFIXES)]
    if not module._diff or not lines:
        return {}
    return {"diff": {"prepared": "\n".join(lines)}}


def main():
    argument_spec = dict(
        spec=dict(type="dict", required=True),
        prune=dict(type="bool", default=False),
    )
    argument_spec.update(RGW_COMMON_ARGS)

    module = AnsibleModule(argument_spec=argument_spec, supports_check_mode=True)

    spec = dict(module.params["spec"])
    spec["prune"] = module.params["prune"]
    rgw = RGW(module)

    try:
        roles = compose(spec)
    except SpecError as exc:
        module.fail_json(msg="invalid IAM spec: %s" % exc)
    except KeyError as exc:
        module.fail_json(msg="invalid IAM spec: missing key %s" % exc)

    def run(*args):
        rc, _, out, err = rgw.run(*args, json_out=False, check_rc=False)
        return rc, out, err

    reconciler = Reconciler(spec, Runner(run), dry_run=module.check_mode)
    try:
        reconciler.run(roles)
    except (IAMError, RGWError) as exc:
        module.fail_json(msg="IAM reconcile failed: %s" % getattr(exc, "msg", exc),
                         changed=reconciler.changed, actions=reconciler.actions,
                         exec_mode_used=rgw.exec_mode_used,
                         **changeset(module, reconciler.actions))

    module.exit_json(changed=reconciler.changed,
                     actions=reconciler.actions,
                     roles=[r["name"] for r in roles],
                     required_groups=sorted({grant_path(spec, r) for r in roles}),
                     exec_mode_used=rgw.exec_mode_used,
                     **changeset(module, reconciler.actions))


if __name__ == "__main__":
    main()
