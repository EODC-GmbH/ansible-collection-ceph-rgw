#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: rgw_orch_restart
short_description: Restart a cephadm-orchestrated service
version_added: "0.2.0"
description:
  - Runs C(ceph orch restart) through the same execution layer as the rest of the
    collection, so a host that can reach C(radosgw-admin) natively or in a container does
    not also need C(cephadm) installed for the restart to work.
  - Always reports changed. C(ceph orch restart) is asynchronous and reports only that the
    request was scheduled, so there is nothing to compare and nothing to preview - under
    C(--check) the restart is reported but not issued.
options:
  service:
    description: The orchestrator service name, as C(ceph orch ls) reports it.
    type: str
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
- name: Restart RGW so it re-reads the OIDC provider thumbprints
  underknowledge.ceph_rgw.rgw_orch_restart:
    service: rgw.rgw
"""

RETURN = r"""
service:
  description: The service the restart was requested for.
  returned: always
  type: str
  sample: rgw.rgw
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


def main():
    argument_spec = dict(service=dict(type="str", required=True))
    argument_spec.update(RGW_COMMON_ARGS)

    module = AnsibleModule(argument_spec=argument_spec, supports_check_mode=True)

    service = module.params["service"]
    ceph = RGW(module, binary="ceph")

    if not module.check_mode:
        try:
            ceph.run("orch", "restart", service, json_out=False)
        except RGWError as exc:
            module.fail_json(msg="ceph orch restart %s failed: %s" % (service, exc.msg),
                             rc=exc.rc, stderr=exc.stderr, cmd=exc.cmd)

    module.exit_json(changed=True, service=service,
                     exec_mode_used=ceph.exec_mode_used)


if __name__ == "__main__":
    main()
