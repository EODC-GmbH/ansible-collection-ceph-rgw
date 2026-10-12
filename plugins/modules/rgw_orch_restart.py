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
version_added: "0.3.0"
description:
  - Runs C(ceph orch restart) through the same execution layer as the rest of the
    collection, so a host that can reach C(radosgw-admin) natively or in a container does
    not also need C(cephadm) installed for the restart to work.
  - Always reports changed.
    Under C(--check) the restart is reported but not issued.
  - C(ceph orch restart) only schedules the restart,
    and the old daemons keep answering for a moment after it returns.
    The module records each daemon's C(started) time and container id from C(ceph orch ps),
    then polls C(ceph orch ps --refresh)
    until every daemon of the service is C(running) with a new start time or container id.
    Unchanged values are cached from before the restart.
  - A daemon can report C(running) a few seconds before it accepts connections,
    so a task that uses the endpoint next should also wait for it to answer.
options:
  service:
    description: The orchestrator service name, as C(ceph orch ls) reports it.
    type: str
    required: true
  wait_timeout:
    description:
      - Seconds to wait for every daemon of O(service) to be running again after the restart.
        The task fails if they are not.
      - C(0) returns as soon as the restart is requested.
    type: int
    default: 300
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
- name: Restart RGW after an STS config change
  underknowledge.ceph_rgw.rgw_orch_restart:
    service: rgw.rgw

- name: Request the restart without waiting for it
  underknowledge.ceph_rgw.rgw_orch_restart:
    service: rgw.rgw
    wait_timeout: 0
"""

RETURN = r"""
service:
  description: The service the restart was requested for.
  returned: always
  type: str
  sample: rgw.rgw
daemons:
  description:
    - The daemons seen running again after the restart, with their new start time.
    - Empty when O(wait_timeout=0) or under C(--check).
  returned: always
  type: list
  elements: dict
  sample: [{"daemon_name": "rgw.rgw.ceph0.grykgg", "started": "2026-10-05T14:25:46.579509Z"}]
exec_mode_used:
  description: Which execution path actually ran.
  returned: always
  type: str
  sample: container
"""

import json
import time

from ansible.module_utils.basic import AnsibleModule
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw import (
    RGW, RGW_COMMON_ARGS, RGWError,
)

POLL_INTERVAL = 5


def list_daemons(ceph, service, refresh=False):
    """{daemon_name: daemon} for the service, from `ceph orch ps`.

    --refresh only asks cephadm to rescan the hosts; the answer still comes from the cache.
    """
    args = ["orch", "ps", "--service_name", service, "--format", "json"]
    if refresh:
        args.append("--refresh")
    _, _, out, _ = ceph.run(*args, json_out=False)
    lines = [line for line in out.splitlines() if line.strip()]
    try:
        daemons = json.loads(lines[-1]) if lines else []
    except ValueError:
        raise RGWError("ceph orch ps returned no JSON: %s" % out.strip()[-200:])
    return dict((d.get("daemon_name"), d) for d in daemons)


def identity(daemon):
    return daemon.get("started"), daemon.get("container_id")


def pending(before, after):
    """Daemons of `after` that are not yet a running, restarted instance."""
    waiting = []
    for name, daemon in sorted(after.items()):
        old = before.get(name)
        if daemon.get("status_desc") != "running":
            waiting.append("%s is %s" % (name, daemon.get("status_desc") or "unknown"))
        elif old is not None and identity(old) == identity(daemon):
            waiting.append("%s has not restarted yet (started %s)"
                           % (name, daemon.get("started")))
    if before and not after:
        waiting.append("no daemons listed for the service")
    return waiting


def wait_restarted(module, ceph, service, before, timeout):
    """Poll until every daemon is running again, or fail after timeout seconds."""
    deadline = time.monotonic() + timeout
    while True:
        after = list_daemons(ceph, service, refresh=True)
        waiting = pending(before, after)
        if not waiting:
            return [dict(daemon_name=name, started=d.get("started"))
                    for name, d in sorted(after.items())]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            module.fail_json(
                msg="%s did not come back within %ds of ceph orch restart: %s"
                    % (service, timeout, "; ".join(waiting)),
                service=service, exec_mode_used=ceph.exec_mode_used)
        time.sleep(min(POLL_INTERVAL, remaining))


def main():
    argument_spec = dict(service=dict(type="str", required=True),
                         wait_timeout=dict(type="int", default=300))
    argument_spec.update(RGW_COMMON_ARGS)

    module = AnsibleModule(argument_spec=argument_spec, supports_check_mode=True)

    service = module.params["service"]
    timeout = module.params["wait_timeout"]
    ceph = RGW(module, binary="ceph")
    daemons = []

    if not module.check_mode:
        try:
            before = list_daemons(ceph, service) if timeout > 0 else {}
            ceph.run("orch", "restart", service, json_out=False)
            if timeout > 0:
                daemons = wait_restarted(module, ceph, service, before, timeout)
        except RGWError as exc:
            module.fail_json(msg="ceph orch restart %s failed: %s" % (service, exc.msg),
                             rc=exc.rc, stderr=exc.stderr, cmd=exc.cmd)

    module.exit_json(changed=True, service=service, daemons=daemons,
                     exec_mode_used=ceph.exec_mode_used)


if __name__ == "__main__":
    main()
