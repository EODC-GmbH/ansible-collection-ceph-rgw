#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: rgw_key_holder_info
short_description: Find the Ceph RGW user that holds an access key
version_added: "0.7.0"
description:
  - Runs C(radosgw-admin user info --access-key)
    and reports which user holds the key, if any. Read-only.
  - RGW answers C(AccessDenied) both for an access key no user holds
    and for a user without permission.
    M(underknowledge.ceph_rgw.rgw_bucket), M(underknowledge.ceph_rgw.rgw_oidc_provider)
    and M(underknowledge.ceph_rgw.rgw_user_policy) take RV(key_holder) to tell the two apart,
    so they can run on a host without C(radosgw-admin).
options:
  access_key:
    description: The access key to look up.
    type: str
    required: true
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
  - Needs the cluster admin keyring (C(become)).
  - Fails when the cluster does not answer, rather than reporting the key as unheld.
author:
  - Underknowledge (@Underknowledge)
"""

EXAMPLES = r"""
- name: Look up who holds the tenant's root key
  underknowledge.ceph_rgw.rgw_key_holder_info:
    access_key: "{{ vault_rgw_tenants_aviato_access_key }}"
  become: true
  register: aviato_holder

- name: Create the tenant's buckets from the control host
  underknowledge.ceph_rgw.rgw_bucket:
    names: [aviato-data]
    endpoint_url: https://objects.example.com
    access_key: "{{ vault_rgw_tenants_aviato_access_key }}"
    secret_key: "{{ vault_rgw_tenants_aviato_secret_key }}"
    key_holder: "{{ aviato_holder.key_holder }}"
  delegate_to: localhost
"""

RETURN = r"""
key_holder:
  description:
    - Whether a user holds the key, and if so which one. Secret keys are not returned.
  returned: always
  type: dict
  contains:
    found:
      description: Whether any user holds the access key.
      type: bool
    user_id:
      description: The holder's uid. Only when RV(key_holder.found).
      type: str
    account_id:
      description: The holder's account, empty for a user outside any account.
      type: str
    suspended:
      description: Whether the holder is suspended.
      type: bool
  sample: {found: true, user_id: aviatoroot, account_id: RGW00000000000000001, suspended: false}
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
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_read import (
    read_key_holder,
)
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_refusal import (
    holder_summary,
)


def main():
    argument_spec = dict(access_key=dict(type="str", required=True, no_log=True))
    argument_spec.update(RGW_COMMON_ARGS)
    module = AnsibleModule(argument_spec=argument_spec, supports_check_mode=True)

    rgw = RGW(module)
    try:
        info = read_key_holder(rgw, module.params["access_key"])
    except RGWError as exc:
        module.fail_json(msg="radosgw-admin could not tell whether any user holds the "
                             "access key: %s" % exc.msg,
                         rc=exc.rc, stderr=exc.stderr, cmd=exc.cmd)

    module.exit_json(changed=False, key_holder=holder_summary(info),
                     exec_mode_used=rgw.exec_mode_used)


if __name__ == "__main__":
    main()
