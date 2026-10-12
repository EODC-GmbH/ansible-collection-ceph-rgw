#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: rgw_config
short_description: Set one Ceph cluster configuration option, on drift only
version_added: "0.3.0"
description:
  - Reads C(ceph config get <who> <option>) and runs C(ceph config set) only when the
    stored value differs, so a converge that changes nothing reports nothing and no
    handler is notified.
  - Runs C(ceph), not C(radosgw-admin), through the same native/container/cephadm
    resolution as the rest of the collection.
  - O(value) and O(secret_value) do the same thing. They are separate because a C(no_log)
    value masks every matching string in the task's output.
options:
  who:
    description: The configuration entity, as C(ceph config) names it.
    type: str
    default: client.rgw
  option:
    description: The option name, for example C(rgw_sts_key) or C(rgw_s3_auth_use_sts).
    type: str
    required: true
  value:
    description:
      - The value to ensure, as a string. Mutually exclusive with O(secret_value); exactly
        one of the two is required.
      - Booleans are the strings C(true) and C(false), which is what C(ceph config get)
        prints back.
    type: str
  secret_value:
    description:
      - The value to ensure, when it is a secret such as an STS signing key.
      - Identical to O(value) except that it is C(no_log) and no diff is emitted.
    type: str
  exec_mode:
    description: How to reach C(ceph).
    type: str
    choices: [auto, native, container, cephadm]
    default: auto
  fsid:
    description: Cluster fsid. Read from C(/etc/ceph/ceph.conf) when not given.
    type: str
  image:
    description: Ceph container image.
    type: str
  container_runtime:
    description: Container runtime to use.
    type: str
    choices: [docker, podman]
  cluster:
    description: Ceph cluster name.
    type: str
    default: ceph
  keyring:
    description: Path to a ceph keyring.
    type: str
author:
  - Underknowledge (@Underknowledge)
notes:
  - O(secret_value) is passed to C(ceph config set) on argv. On the Ansible side it is
    C(no_log).
  - C(changed) is what tells a caller to restart RGW. This module notifies nothing and
    restarts nothing itself.
  - C(ceph config get) answers an unset option with its compiled-in default, so setting an
    option to its default reports no change.
requirements:
  - the C(ceph) CLI reachable natively, in a container image, or through C(cephadm shell)
"""

EXAMPLES = r"""
- name: Enable STS auth on the S3 endpoint
  underknowledge.ceph_rgw.rgw_config:
    option: rgw_s3_auth_use_sts
    value: "true"
  notify: Restart RGW

- name: Set the STS session-token signing key
  underknowledge.ceph_rgw.rgw_config:
    option: rgw_sts_key
    secret_value: "{{ vault_rgw_sts_key }}"
  notify: Restart RGW

- name: Raise a setting on one daemon only
  underknowledge.ceph_rgw.rgw_config:
    who: client.rgw.rgw.host1
    option: rgw_thread_pool_size
    value: "1024"
"""

RETURN = r"""
value:
  description: The option's value after the run.
  returned: when O(value) was used, never for O(secret_value)
  type: str
  sample: "true"
exec_mode_used:
  description: Which execution path actually ran.
  returned: always
  type: str
  sample: container
"""

from ansible.module_utils.basic import AnsibleModule
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw import (
    RGW, RGW_COMMON_ARGS, RGWError, config_get, diff_of,
)


def main():
    argument_spec = dict(
        who=dict(type="str", default="client.rgw"),
        option=dict(type="str", required=True),
        value=dict(type="str"),
        secret_value=dict(type="str", no_log=True),
    )
    argument_spec.update(RGW_COMMON_ARGS)

    module = AnsibleModule(
        argument_spec=argument_spec,
        mutually_exclusive=[("value", "secret_value")],
        required_one_of=[("value", "secret_value")],
        supports_check_mode=True,
    )

    p = module.params
    who, option = p["who"], p["option"]
    secret = p["secret_value"] is not None
    wanted = p["secret_value"] if secret else p["value"]

    ceph = RGW(module, binary="ceph")

    try:
        current = config_get(ceph, who, option)
        changed = current != wanted
        if changed and not module.check_mode:
            ceph.run("config", "set", who, option, wanted, json_out=False)
    except RGWError as exc:
        module.fail_json(msg="ceph config failed for %s %s: %s" % (who, option, exc.msg),
                         rc=exc.rc, stderr=exc.stderr, cmd=exc.cmd)

    result = dict(changed=changed, exec_mode_used=ceph.exec_mode_used)
    if not secret:
        result["value"] = wanted
        result["diff"] = diff_of({option: current}, {option: wanted})
    module.exit_json(**result)


if __name__ == "__main__":
    main()
