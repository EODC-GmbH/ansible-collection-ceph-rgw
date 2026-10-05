# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
# Service-user composition for rgw_iam. Pure, and the same code rgw_iam_sync composes roles
# with, so a service user and a role holding the same capabilities get the same policy.

from __future__ import absolute_import, division, print_function

__metaclass__ = type

from ansible.errors import AnsibleFilterError
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_iam import (
    SpecError, compose_users, service_user_name,
)


def rgw_service_users(spec):
    """The IAM spec's declared service users, each with its composed permission policy.

    Raises on an invalid declaration (unknown capability, foreign bucket, delegated IAM),
    so a typo fails the run naming the user instead of reaching RGW.
    """
    try:
        return compose_users(spec)
    except SpecError as exc:
        raise AnsibleFilterError(str(exc))


def rgw_service_user_name(name, tenant):
    """uid == display name == IAM UserName of a service user: iam-<tenant>-svc-<name>."""
    return service_user_name(tenant, name)


class FilterModule:
    """Ceph RGW service-user filters."""

    def filters(self):
        return {
            "rgw_service_users": rgw_service_users,
            "rgw_service_user_name": rgw_service_user_name,
        }
