#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: rgw_oidc_provider
short_description: Register an OIDC realm as an RGW OIDC provider in one account
version_added: "0.3.0"
description:
  - Creates, replaces or removes the OIDC provider of a single RGW account, so that
    account's IAM roles can federate web-identity logins from that realm.
  - The resulting ARN is C(arn:aws:iam::<account>:oidc-provider/<host>/<path>), which is
    what each tenant's role trust policy matches on.
options:
  url:
    description:
      - The realm issuer URL including the scheme, as in the C(iss) claim of the tokens.
      - An existing provider is matched on host plus path. A trailing slash is rejected.
    type: str
    required: true
  client_ids:
    description:
      - Accepted audiences, for example V(ceph-s3).
      - Ignored when O(state=absent).
    type: list
    elements: str
    default: []
  thumbprints:
    description:
      - SHA1 fingerprints of the realm's JWKS signing certificate, 40 hex characters
        each. Case is normalised to lower.
      - Ignored when O(state=absent).
    type: list
    elements: str
    default: []
  endpoint_url:
    description: The RGW endpoint serving the IAM API, for example V(http://192.168.70.10:8080).
    type: str
    required: true
  region:
    description: SigV4 signing region. RGW ignores it.
    type: str
    default: us-east-1
  access_key:
    description: The account-root S3 access key of the account to register in.
    type: str
    required: true
  secret_key:
    description: The account-root S3 secret key.
    type: str
    required: true
  state:
    description: Whether the provider should exist.
    type: str
    choices: [present, absent]
    default: present
  validate_certs:
    description: Verify the endpoint's TLS certificate.
    type: bool
    default: true
  ca_bundle:
    description:
      - CA bundle to verify the endpoint's TLS certificate against, on the host running
        the module. Needed when RGW serves a certificate from a private CA.
      - Takes precedence over O(validate_certs) when set.
    type: path
  exec_mode:
    description:
      - How to reach C(radosgw-admin).
        The module calls it only after the IAM API refuses the keys,
        to see whether any user holds the access key.
    type: str
    choices: [auto, native, container, cephadm]
    default: auto
  fsid:
    description: Cluster fsid.
      Read from C(/etc/ceph/ceph.conf) when not given.
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
requirements:
  - boto3
notes:
  - OIDC providers are IAM resources scoped to the calling credentials' account, and Ceph
    squid has no working C(radosgw-admin oidc-provider) command, so this module uses the
    IAM API at O(endpoint_url).
  - boto3 must be importable by the interpreter running the module, on the Ceph host.
  - An empty or malformed thumbprint set is refused before any change is made; such a
    provider would strand every federated login.
  - RGW has no C(UpdateOpenIDConnectProviderThumbprint), so drift in O(thumbprints) or
    O(client_ids) is applied as delete plus recreate. If the recreate fails, the previous
    client ids and thumbprints are put back and the task fails reporting both errors.
  - RGW reads the provider on each C(AssumeRoleWithWebIdentity),
    so a create or replace applies to the next request without a restart.
  - RGW answers C(AccessDenied) both for an unknown key and for a user without permission.
    After a refusal the module runs C(radosgw-admin user info --access-key)
    to see whether any user holds the key,
    which needs the cluster admin keyring (use C(become)).
    If none does, check mode reports V(would create)
    and O(state=absent) reports the provider as gone with its account.
    Otherwise the task fails.
author:
  - Underknowledge (@Underknowledge)
"""

EXAMPLES = r"""
- name: Register the OIDC realm as a provider per tenant
  underknowledge.ceph_rgw.rgw_oidc_provider:
    url: "{{ rgw_tenants_oidc.provider_url }}"
    client_ids: "{{ rgw_tenants_oidc.client_ids }}"
    thumbprints: "{{ rgw_tenants_oidc.thumbprints }}"
    endpoint_url: "{{ rgw_tenants_endpoint }}"
    region: "{{ rgw_tenants_s3_region }}"
    access_key: "{{ lookup('vars', 'vault_rgw_tenants_' ~ item.name ~ '_access_key') }}"
    secret_key: "{{ lookup('vars', 'vault_rgw_tenants_' ~ item.name ~ '_secret_key') }}"
  loop: "{{ _rgw_tenants_present }}"
  loop_control:
    label: "{{ item.name }} ({{ item.account_id }})"

- name: Drop the provider of a decommissioned tenant
  underknowledge.ceph_rgw.rgw_oidc_provider:
    url: "{{ rgw_tenants_oidc.provider_url }}"
    endpoint_url: "{{ rgw_tenants_endpoint }}"
    access_key: "{{ vault_rgw_tenants_aviato_access_key }}"
    secret_key: "{{ vault_rgw_tenants_aviato_secret_key }}"
    state: absent
"""

RETURN = r"""
arn:
  description:
    - The provider ARN after the run, or the ARN that was deleted.
    - Null when the provider does not exist, when the account is already gone, or when
      check mode could not probe.
  returned: always
  type: str
  sample: arn:aws:iam::RGW00000000000000001:oidc-provider/sso.example.com/realms/ceph
result:
  description:
    - One of V(created), V(replaced), V(exists), V(deleted), V(absent), or in check mode
      V(would create), V(would replace), V(would delete).
  returned: always
  type: str
  sample: created
rolled_back:
  description: Whether a failed recreate was rolled back to the previous provider.
  returned: on a failed replace
  type: bool
  sample: true
probed:
  description:
    - Whether the IAM endpoint was queried.
      False when no user holds the access key, in check mode or with O(state=absent).
  returned: always
  type: bool
  sample: true
"""

import re

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw import (
    RGW, RGW_COMMON_ARGS, diff_of,
)
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_refusal import (
    REFUSED_CODES, error_code, exit_if_suspended_preview, key_holder, refused_msg,
)

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    HAS_BOTO3 = False
else:
    HAS_BOTO3 = True

SHA1_HEX = re.compile(r"^[0-9a-f]{40}$")


def invalid_thumbprints(thumbprints):
    """The reason this thumbprint set is unusable, or None."""
    if not thumbprints:
        return "thumbprints is empty; refusing to register a thumbprint-less provider"
    bad = [t for t in thumbprints if not SHA1_HEX.match(t)]
    if bad:
        return ("thumbprints malformed (need 40-hex SHA1): %s. Compute them from "
                "<issuer>/protocol/openid-connect/certs; see 'Compute the OIDC thumbprint' "
                "in the collection README"
                % bad)
    return None


def find_arn(client, url):
    """The ARN of the provider whose host and path match url, or None.

    Matched on the ARN suffix: the account id inside the ARN is not among the arguments.
    """
    suffix = url.split("://", 1)[-1]
    listed = client.list_open_id_connect_providers()
    for provider in listed.get("OpenIDConnectProviderList") or []:
        if provider["Arn"].split(":oidc-provider/", 1)[-1] == suffix:
            return provider["Arn"]
    return None


def create_provider(client, url, client_ids, thumbprints):
    created = client.create_open_id_connect_provider(
        Url=url, ClientIDList=client_ids, ThumbprintList=thumbprints,
    )
    return created["OpenIDConnectProviderArn"]


def snapshot(present, client_ids, thumbprints):
    """The comparable shape of a provider, for idempotence and for the diff."""
    return {
        "present": present,
        "client_ids": sorted(client_ids),
        "thumbprints": sorted(t.lower() for t in thumbprints),
    }


def holder_of(module):
    """The `user info` of the access key's holder, or None. Called after IAM refused it."""
    return key_holder(module, RGW(module), "the IAM request")


def handle_absent(module, client, url, holder=holder_of):
    """Delete the provider matched by url. A key no user holds means the account is gone."""
    try:
        arn = find_arn(client, url)
        if arn is None:
            module.exit_json(changed=False, probed=True, arn=None, result="absent",
                             diff=diff_of({"present": False}, {"present": False}))
        if module.check_mode:
            module.exit_json(changed=True, probed=True, arn=arn, result="would delete",
                             diff=diff_of({"present": True}, {"present": False}))
        client.delete_open_id_connect_provider(OpenIDConnectProviderArn=arn)
    except ClientError as exc:
        code = error_code(exc)
        if code not in REFUSED_CODES:
            module.fail_json(msg="removing the OIDC provider for %s failed (%s): %s"
                                 % (url, code, exc))
        owner = holder(module)
        exit_if_suspended_preview(module, owner, "removing the OIDC provider for %s" % url,
                                  arn=None, result="not probed")
        if owner is not None:
            module.fail_json(msg=refused_msg("removing the OIDC provider for %s" % url,
                                             code, owner))
        module.exit_json(
            changed=False, probed=False, arn=None, result="absent",
            diff=diff_of({"present": False}, {"present": False}),
            msg="IAM answered %s and no RGW user holds this access key, so the account "
                "for %s is already gone, and its provider with it" % (code, url))
    except BotoCoreError as exc:
        module.fail_json(msg="removing the OIDC provider for %s failed: %s" % (url, exc))
    module.exit_json(changed=True, probed=True, arn=arn, result="deleted",
                     diff=diff_of({"present": True}, {"present": False}))


def replace(module, client, url, arn, before, want, previous):
    """Delete and recreate the provider, rolling back if the recreate fails.

    RGW has no UpdateOpenIDConnectProviderThumbprint, so drift is applied as delete plus
    create. `previous` is the pair exactly as RGW returned it, so a rollback restores it
    byte for byte rather than the normalised copy the comparison uses.
    """
    prev_cids, prev_tps = previous
    try:
        client.delete_open_id_connect_provider(OpenIDConnectProviderArn=arn)
    except (BotoCoreError, ClientError) as exc:
        module.fail_json(msg="replacing the OIDC provider for %s failed on delete (%s); "
                             "the previous provider is still in place"
                             % (url, exc), arn=arn, rolled_back=False, changed=False,
                         probed=True, result="exists", diff=diff_of(before, before))
    try:
        new_arn = create_provider(client, url, want["client_ids"], want["thumbprints"])
    except Exception as exc:  # noqa: BLE001 - whatever failed, restore the known-good one
        try:
            restored = create_provider(client, url, prev_cids, prev_tps)
        except Exception as restore_exc:  # noqa: BLE001
            module.fail_json(
                msg="recreating the OIDC provider for %s failed (%s) and the rollback "
                    "failed too (%s); manual intervention required: the tenant is left "
                    "without an OIDC provider" % (url, exc, restore_exc),
                arn=None, rolled_back=False, changed=True, probed=True,
                result="replaced", diff=diff_of(before, {"present": False}))
        module.fail_json(
            msg="recreating the OIDC provider for %s failed (%s); rolled back to the "
                "previous provider thumbprints=%s"
                % (url, exc, prev_tps),
            arn=restored, rolled_back=True, changed=True, probed=True,
            result="replaced", diff=diff_of(before, before))
    module.exit_json(changed=True, probed=True, arn=new_arn, result="replaced",
                     diff=diff_of(before, want))


def handle_present(module, client, url, want, holder=holder_of):
    """Create the provider, replace it on drift, or leave it alone."""
    try:
        arn = find_arn(client, url)
    except ClientError as exc:
        code = error_code(exc)
        if code not in REFUSED_CODES:
            module.fail_json(msg="listing OIDC providers at %s failed (%s): %s"
                                 % (url, code, exc))
        owner = holder(module)
        exit_if_suspended_preview(module, owner, "listing OIDC providers for %s" % url,
                                  arn=None, result="not probed")
        if owner is None and module.check_mode:
            # Greenfield tenant: the account-root user does not exist yet.
            module.exit_json(
                changed=True, probed=False, arn=None, result="would create",
                diff=diff_of({"present": "unknown"}, want),
                msg="would create; prerequisite account-root user for this access key "
                    "absent, not probed")
        module.fail_json(msg=refused_msg("listing OIDC providers for %s" % url, code, owner))
    except BotoCoreError as exc:
        module.fail_json(msg="listing OIDC providers for %s failed: %s" % (url, exc))

    if arn is None:
        before = snapshot(False, [], [])
        if module.check_mode:
            module.exit_json(changed=True, probed=True, arn=None, result="would create",
                             diff=diff_of(before, want))
        try:
            arn = create_provider(client, url, want["client_ids"], want["thumbprints"])
        except (BotoCoreError, ClientError) as exc:
            module.fail_json(msg="creating the OIDC provider for %s failed: %s"
                                 % (url, exc), arn=None, changed=False)
        module.exit_json(changed=True, probed=True, arn=arn, result="created",
                         diff=diff_of(before, want))

    try:
        current = client.get_open_id_connect_provider(OpenIDConnectProviderArn=arn)
    except (BotoCoreError, ClientError) as exc:
        module.fail_json(msg="reading the OIDC provider %s failed: %s" % (arn, exc))
    prev_cids = current.get("ClientIDList") or []
    prev_tps = current.get("ThumbprintList") or []
    before = snapshot(True, prev_cids, prev_tps)

    if before == want:
        module.exit_json(changed=False, probed=True, arn=arn, result="exists",
                         diff=diff_of(before, want))

    if module.check_mode:
        module.exit_json(changed=True, probed=True, arn=arn, result="would replace",
                         diff=diff_of(before, want))

    replace(module, client, url, arn, before, want, (prev_cids, prev_tps))


def main():
    argument_spec = dict(
        url=dict(type="str", required=True),
        client_ids=dict(type="list", elements="str", default=[]),
        thumbprints=dict(type="list", elements="str", default=[]),
        endpoint_url=dict(type="str", required=True),
        region=dict(type="str", default="us-east-1"),
        access_key=dict(type="str", required=True, no_log=True),
        secret_key=dict(type="str", required=True, no_log=True),
        state=dict(type="str", default="present", choices=["present", "absent"]),
        validate_certs=dict(type="bool", default=True),
        ca_bundle=dict(type="path"),
    )
    argument_spec.update(RGW_COMMON_ARGS)

    module = AnsibleModule(argument_spec=argument_spec, supports_check_mode=True)

    if not HAS_BOTO3:
        module.fail_json(msg=missing_required_lib("boto3"))

    p = module.params
    url = p["url"]
    client_ids = [c.strip() for c in p["client_ids"] if c and c.strip()]
    thumbprints = [t.strip().lower() for t in p["thumbprints"] if t and t.strip()]

    # Rejected rather than trimmed: rgw_iam derives the trust condition key from the same
    # value, and a slash there makes the key unmatchable.
    if url != url.rstrip("/"):
        module.fail_json(msg="url is '%s'. Drop the trailing slash: providers are matched "
                             "on the ARN suffix, which RGW stores without one, so this "
                             "would create a duplicate provider on every run." % url)
    if p["state"] == "present":
        reason = invalid_thumbprints(thumbprints)
        if reason:
            module.fail_json(msg="%s (provider %s)" % (reason, url))

    try:
        client = boto3.client(
            "iam",
            endpoint_url=p["endpoint_url"],
            region_name=p["region"],
            aws_access_key_id=p["access_key"],
            aws_secret_access_key=p["secret_key"],
            verify=p["ca_bundle"] or p["validate_certs"],
        )
    except (BotoCoreError, ValueError) as exc:
        module.fail_json(msg="could not build an IAM client for %s: %s"
                             % (p["endpoint_url"], exc))

    if p["state"] == "absent":
        handle_absent(module, client, url)

    handle_present(module, client, url, snapshot(True, client_ids, thumbprints))


if __name__ == "__main__":
    main()
