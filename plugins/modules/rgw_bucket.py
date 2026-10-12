#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: rgw_bucket
short_description: Manage a Ceph RGW tenant's S3 buckets with its account-root keys
version_added: "0.3.0"
description:
  - Creates or removes the S3 buckets of one RGW account, one task for the whole set.
  - Buckets belong to a user in RGW, so they are made over S3 with the tenant's
    account-root credentials, not with C(radosgw-admin).
  - Existence is probed with C(ListBuckets) and C(HeadBucket), because RGW answers 200 to
    C(CreateBucket) for a bucket the caller already owns.
options:
  name:
    description: A single bucket name. Mutually exclusive with O(names).
    type: str
  names:
    description: Bucket names, so one task handles a tenant's whole set.
    type: list
    elements: str
  endpoint_url:
    description: The RGW S3 endpoint, for example V(http://rgw.example.com:8080).
    type: str
    required: true
  region:
    description:
      - SigV4 signing region only, not sent as a C(LocationConstraint). RGW places a bucket
        in the zonegroup the endpoint belongs to.
    type: str
    default: us-east-1
  access_key:
    description: The account-root S3 access key.
    type: str
    required: true
  secret_key:
    description: The account-root S3 secret key.
    type: str
    required: true
  state:
    description: Whether the buckets should exist.
    type: str
    choices: [present, absent]
    default: present
  purge_objects:
    description:
      - When removing, delete every object in the bucket first.
      - Without it a non-empty bucket fails with RGW's C(BucketNotEmpty).
    type: bool
    default: false
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
  key_holder:
    description:
      - The RV(underknowledge.ceph_rgw.rgw_key_holder_info#module:key_holder) result
        for O(access_key), read on a Ceph host before this task.
      - Consulted only after a refusal. Without it a refusal fails the task.
    type: dict
    suboptions:
      found:
        description: Whether any user holds the access key.
        type: bool
        required: true
      user_id:
        description: The holder's uid.
        type: str
      account_id:
        description: The holder's account.
        type: str
      suspended:
        description: Whether the holder is suspended.
        type: bool
        default: false
requirements:
  - boto3
notes:
  - boto3 must be importable by the interpreter running the module.
    It needs no Ceph tooling, so it can run on any host that reaches O(endpoint_url).
  - Bucket names are cluster-global in RGW. A name held by another account fails the
    task, naming the bucket.
  - O(purge_objects) deletes current object versions only. A versioned bucket with
    non-current versions or delete markers still fails with C(BucketNotEmpty); remove it
    with C(radosgw-admin bucket rm --purge-objects).
  - RGW answers C(AccessDenied) both for an unknown key and for a user without permission.
    After a refusal the module reads O(key_holder). If no user holds the key, check mode
    reports the create it would do and O(state=absent) reports the buckets as gone.
    Otherwise the task fails.
author:
  - Underknowledge (@Underknowledge)
"""

EXAMPLES = r"""
- name: Look up who holds the tenant's account-root key, on a Ceph host
  underknowledge.ceph_rgw.rgw_key_holder_info:
    access_key: "{{ vault_rgw_tenants_aviato_access_key }}"
  become: true
  register: aviato_holder

- name: Create the tenant's buckets as its account-root
  underknowledge.ceph_rgw.rgw_bucket:
    names: [aviato-data, aviato-logs]
    endpoint_url: "{{ rgw_tenants_endpoint }}"
    region: "{{ rgw_tenants_s3_region }}"
    access_key: "{{ vault_rgw_tenants_aviato_access_key }}"
    secret_key: "{{ vault_rgw_tenants_aviato_secret_key }}"
    key_holder: "{{ aviato_holder.key_holder }}"
  delegate_to: localhost

- name: Remove a decommissioned tenant's bucket and everything in it
  underknowledge.ceph_rgw.rgw_bucket:
    name: aviato-backups
    endpoint_url: "{{ rgw_tenants_endpoint }}"
    access_key: "{{ vault_rgw_tenants_aviato_access_key }}"
    secret_key: "{{ vault_rgw_tenants_aviato_secret_key }}"
    key_holder: "{{ aviato_holder.key_holder }}"
    state: absent
    purge_objects: true
  delegate_to: localhost
"""

RETURN = r"""
buckets:
  description: One entry per requested bucket, in the order given.
  returned: always
  type: list
  elements: dict
  contains:
    name:
      description: The bucket name.
      type: str
    result:
      description: What happened to it.
      type: str
      sample: created
    changed:
      description: Whether this bucket changed.
      type: bool
    objects_deleted:
      description: Objects removed by O(purge_objects).
      type: int
  sample:
    - {name: aviato-data, result: created, changed: true}
    - {name: aviato-logs, result: exists, changed: false}
probed:
  description:
    - Whether the S3 endpoint was queried. False when no user holds the access key, in
      check mode or with O(state=absent).
  returned: always
  type: bool
  sample: true
"""

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw import (
    diff_of,
)
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_refusal import (
    KEY_HOLDER_ARGS, REFUSED_CODES, error_code, exit_if_suspended_preview, key_holder,
    refused_msg,
)

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    HAS_BOTO3 = False
else:
    HAS_BOTO3 = True

ABSENT_CODES = ("404", "NoSuchBucket")
# Bucket names are cluster-global, so a 403 on HeadBucket means another account holds it.
OWNED_ELSEWHERE_CODES = ("403", "AccessDenied", "Forbidden")
DELETE_BATCH = 1000


class ProbeError(Exception):
    """A probe answer to surface instead of acting on.

    refused holds the error code when S3 refused the credentials, else None.
    """

    def __init__(self, msg, refused=None):
        super(ProbeError, self).__init__(msg)
        self.msg = msg
        self.refused = refused


def http_status(exc):
    response = getattr(exc, "response", None) or {}
    return (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")


def owned_buckets(client):
    """The names this account owns, as one ListBuckets.

    Also the credential check, so it runs first. HeadBucket is a HEAD request, so a failure
    carries no body and no S3 error code, and RGW answers 403 both for a key that does not
    exist and for a name another account holds. ListBuckets is a GET and names no bucket, so
    a refusal there is about the credentials.
    """
    try:
        listing = client.list_buckets()
    except ClientError as exc:
        code = error_code(exc)
        # Squid answers AccessDenied for an unknown key and UserSuspended for a suspended
        # user. Other 403 codes (RequestTimeTooSkewed) come from a user that exists.
        if code in REFUSED_CODES:
            raise ProbeError("ListBuckets at this endpoint was refused (%s)" % code,
                             refused=code)
        raise ProbeError("ListBuckets at this endpoint failed (%s): %s" % (code, exc))
    except BotoCoreError as exc:
        raise ProbeError("ListBuckets at this endpoint failed: %s" % exc)
    return set(b["Name"] for b in listing.get("Buckets") or [])


def bucket_exists(client, name, owned):
    """True when this account owns the bucket, False when it does not exist.

    Raises ProbeError when the name belongs to someone else. HeadBucket is only asked
    about names `owned` lacks, where a 403 is unambiguous: the credentials are proven.
    """
    if name in owned:
        return True
    try:
        client.head_bucket(Bucket=name)
    except ClientError as exc:
        code = error_code(exc)
        if code in ABSENT_CODES or http_status(exc) == 404:
            return False
        if code in OWNED_ELSEWHERE_CODES or http_status(exc) == 403:
            raise ProbeError(
                "HeadBucket on '%s' returned %s. Bucket names are cluster-global in RGW, "
                "so the name is held by another account. Pick another name, or remove the "
                "bucket as its owner." % (name, code))
        raise ProbeError("HeadBucket on '%s' failed (%s): %s" % (name, code, exc))
    # RGW answers 200 to HeadBucket for a name the caller can reach but does not own.
    raise ProbeError(
        "HeadBucket on '%s' succeeded but ListBuckets does not report it, so it belongs "
        "to another account. Bucket names are cluster-global in RGW." % name)


def purge_objects(client, name):
    """Delete every current object version in the bucket. Returns the count deleted."""
    deleted = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=name):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents") or []]
        while keys:
            batch, keys = keys[:DELETE_BATCH], keys[DELETE_BATCH:]
            # DeleteObjects answers 200 and lists per-key failures in Errors.
            result = client.delete_objects(Bucket=name, Delete={"Objects": batch})
            deleted += len(batch) - len(result.get("Errors") or [])
    return deleted


def unprobed_exit(module, names, state, detail=""):
    """S3 refused a key no user holds: report the intent and exit.

    Check mode on a greenfield tenant, where the account-root user does not exist yet, or
    delete mode on a torn-down one, where it is gone and its buckets with it. Teardown
    revokes these keys, so a re-run reaches this step without them.
    """
    would_create = state == "present"
    if would_create:
        msg = "would create; prerequisite account-root user for this access key absent, not probed"
    else:
        msg = ("S3 answered %s and no RGW user holds this access key, so the account-root "
               "user is gone, and its buckets with it" % (detail or "a refusal"))
    module.exit_json(
        changed=would_create,
        probed=False,
        buckets=[{"name": n,
                  "result": "would create" if would_create else "absent",
                  "changed": would_create} for n in names],
        diff=diff_of({name: "unknown" for name in names},
                     {name: state for name in names}),
        msg=msg,
    )


def main():
    argument_spec = dict(
        name=dict(type="str"),
        names=dict(type="list", elements="str"),
        endpoint_url=dict(type="str", required=True),
        region=dict(type="str", default="us-east-1"),
        access_key=dict(type="str", required=True, no_log=True),
        secret_key=dict(type="str", required=True, no_log=True),
        state=dict(type="str", default="present", choices=["present", "absent"]),
        purge_objects=dict(type="bool", default=False),
        validate_certs=dict(type="bool", default=True),
        ca_bundle=dict(type="path"),
    )
    argument_spec.update(KEY_HOLDER_ARGS)

    module = AnsibleModule(
        argument_spec=argument_spec,
        mutually_exclusive=[("name", "names")],
        required_one_of=[("name", "names")],
        supports_check_mode=True,
    )

    if not HAS_BOTO3:
        module.fail_json(msg=missing_required_lib("boto3"))

    p = module.params
    state = p["state"]
    given = [p["name"]] if p["name"] else (p["names"] or [])
    names = [n.strip() for n in given if n and n.strip()]
    if not names:
        module.fail_json(msg="name/names did not yield a single bucket name")

    try:
        client = boto3.client(
            "s3",
            endpoint_url=p["endpoint_url"],
            region_name=p["region"],
            config=Config(signature_version="s3v4"),
            aws_access_key_id=p["access_key"],
            aws_secret_access_key=p["secret_key"],
            verify=p["ca_bundle"] or p["validate_certs"],
        )
    except (BotoCoreError, ValueError) as exc:
        module.fail_json(msg="could not build an S3 client for %s: %s"
                             % (p["endpoint_url"], exc))

    changed = False
    results = []
    before = {}
    after = {}

    try:
        owned = owned_buckets(client)
    except ProbeError as exc:
        if not exc.refused:
            module.fail_json(msg=exc.msg)
        owner = key_holder(module, "ListBuckets", exc.refused)
        exit_if_suspended_preview(module, owner, "ListBuckets", buckets=[])
        if owner is None and (module.check_mode or state == "absent"):
            unprobed_exit(module, names, state, exc.refused)
        module.fail_json(msg=refused_msg("ListBuckets at %s" % p["endpoint_url"],
                                         exc.refused, owner))

    for name in names:
        try:
            exists = bucket_exists(client, name, owned)
        except ProbeError as exc:
            module.fail_json(msg=exc.msg, bucket=name, changed=changed, buckets=results)
        except BotoCoreError as exc:
            module.fail_json(msg="S3 request to %s failed for bucket '%s': %s"
                                 % (p["endpoint_url"], name, exc),
                             changed=changed, buckets=results)

        before[name] = "present" if exists else "absent"

        if state == "present":
            after[name] = "present"
            if exists:
                results.append({"name": name, "result": "exists", "changed": False})
                continue
            if not module.check_mode:
                try:
                    # No CreateBucketConfiguration: RGW rejects a LocationConstraint that
                    # is not its zonegroup's api_name.
                    client.create_bucket(Bucket=name)
                except (BotoCoreError, ClientError) as exc:
                    module.fail_json(msg="CreateBucket failed for '%s' (%s): %s"
                                         % (name, error_code(exc), exc), bucket=name,
                                     changed=changed, buckets=results)
            changed = True
            results.append({"name": name, "result": "created", "changed": True})
            continue

        after[name] = "absent"
        if not exists:
            results.append({"name": name, "result": "absent", "changed": False})
            continue
        removed = 0
        if not module.check_mode:
            try:
                if p["purge_objects"]:
                    removed = purge_objects(client, name)
                client.delete_bucket(Bucket=name)
            except (BotoCoreError, ClientError) as exc:
                code = error_code(exc)
                hint = ""
                if code == "BucketNotEmpty":
                    hint = (" Objects remain after the purge (non-current versions, "
                            "delete markers, or keys DeleteObjects refused); use "
                            "radosgw-admin bucket rm --purge-objects."
                            if p["purge_objects"] else
                            " Set purge_objects to delete its contents first.")
                module.fail_json(msg="DeleteBucket failed for '%s' (%s): %s.%s"
                                     % (name, code, exc, hint), bucket=name,
                                 changed=changed or removed > 0, buckets=results)
        changed = True
        results.append({"name": name, "result": "deleted", "changed": True,
                        "objects_deleted": removed})

    module.exit_json(changed=changed, probed=True, buckets=results,
                     diff=diff_of(before, after))


if __name__ == "__main__":
    main()
