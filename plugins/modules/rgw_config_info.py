#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: rgw_config_info
short_description: Read Ceph runtime configuration options
version_added: "0.2.0"
description:
  - Reads one or more C(ceph config get) values without writing anything, for preflight
    checks that must also run under C(--check).
options:
  who:
    description: The config section to read, for example C(client.rgw).
    type: str
    default: client.rgw
  options:
    description: The option names to read.
    type: list
    elements: str
    required: true
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
"""

EXAMPLES = r"""
- name: Confirm native STS is enabled
  underknowledge.ceph_rgw.rgw_config_info:
    options: [rgw_s3_auth_use_sts]
  register: sts

- name: Assert native STS is on
  ansible.builtin.assert:
    that: sts.config.rgw_s3_auth_use_sts == 'true'
"""

RETURN = r"""
config:
  description: Each requested option mapped to its stored value.
  returned: always
  type: dict
  sample: {rgw_s3_auth_use_sts: "true"}
exec_mode_used:
  description: Which execution path actually ran.
  returned: always
  type: str
  sample: container
"""

from ansible.module_utils.basic import AnsibleModule
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw import (
    RGW, RGW_COMMON_ARGS, RGWError, config_get,
)


def main():
    argument_spec = dict(
        who=dict(type="str", default="client.rgw"),
        options=dict(type="list", elements="str", required=True),
    )
    argument_spec.update(RGW_COMMON_ARGS)

    module = AnsibleModule(argument_spec=argument_spec, supports_check_mode=True)

    who = module.params["who"]
    ceph = RGW(module, binary="ceph")

    try:
        config = dict((option, config_get(ceph, who, option))
                      for option in module.params["options"])
    except RGWError as exc:
        module.fail_json(msg="ceph config get failed for %s: %s" % (who, exc.msg),
                         rc=exc.rc, stderr=exc.stderr, cmd=exc.cmd)

    module.exit_json(changed=False, config=config, exec_mode_used=ceph.exec_mode_used)


if __name__ == "__main__":
    main()
