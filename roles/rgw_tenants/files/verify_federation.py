#!/usr/bin/env python3
"""End-to-end federation smoke test for one tenant-tier.

Walks the whole path an end user takes:
  OIDC login (ROPC) -> access token (aud, org/tier claims)
    -> sts.AssumeRoleWithWebIdentity($tenant-$tier)
      -> temporary creds -> an S3 op the tier allows, plus one it does not.

Needs an IdP login user carrying org=$tenant and tier=$tier claims.
Environment:

  EP           RGW S3/STS/IAM endpoint (e.g. https://s3.example.com)
  IDP          OIDC token endpoint (.../realms/$realm/protocol/openid-connect/token)
  CLIENT       OIDC client id (aud), e.g. ceph-s3
  ACCOUNT_ID   the tenant's RGW account id (RGW + 17 digits)
  TENANT       tenant slug (== the org claim)
  TIER         admin | writer | reader
  USERNAME     login user (default "$TENANT-$TIER")
  PASSWORD     that user's password
  BUCKET       a bucket to exercise (default "$TENANT-data")
  CROSS_BUCKET a bucket in another tenant's account (optional). If set, listing
               it must be denied, which checks the account-isolation boundary
               rather than tier scoping. Unset skips the check.
  AWS_REGION   SigV4 signing region, default us-east-1 (RGW ignores it)

Exit 0 = the tier's allowed op worked, the reader's denied op was denied, and
(when CROSS_BUCKET is set) another tenant's bucket was out of reach.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# What RGW answers when a policy says no. Everything else -- a typo'd bucket name, a 500, an
# expired session token, SlowDown -- is a broken test, not a passing one.
_DENIED = ("AccessDenied", "AccessDeniedException", "Forbidden", "403")


def denied(exc):
    """True only if this error is an authorisation refusal."""
    err = exc.response.get("Error", {}) if getattr(exc, "response", None) else {}
    return str(err.get("Code", "")) in _DENIED

EP = os.environ["EP"]
IDP = os.environ["IDP"]
CLIENT = os.environ["CLIENT"]
ACCOUNT_ID = os.environ["ACCOUNT_ID"]
TENANT = os.environ["TENANT"]
TIER = os.environ["TIER"]
USERNAME = os.environ.get("USERNAME", f"{TENANT}-{TIER}")
PASSWORD = os.environ["PASSWORD"]
BUCKET = os.environ.get("BUCKET", f"{TENANT}-data")
CROSS_BUCKET = os.environ.get("CROSS_BUCKET", "")
REGION = os.environ.get("AWS_REGION", "us-east-1")

_CFG = Config(signature_version="s3v4", s3={"addressing_style": "path"})


def get_token():
    data = urllib.parse.urlencode({
        "grant_type": "password", "client_id": CLIENT,
        "username": USERNAME, "password": PASSWORD, "scope": "openid",
    }).encode()
    with urllib.request.urlopen(IDP, data) as r:  # noqa: S310 - operator-supplied IdP URL
        return json.load(r)["access_token"]


def assume(token):
    sts = boto3.client("sts", endpoint_url=EP, region_name=REGION)
    arn = f"arn:aws:iam::{ACCOUNT_ID}:role/{TENANT}-{TIER}"
    return sts.assume_role_with_web_identity(
        RoleArn=arn, RoleSessionName=f"{TENANT}-{TIER}-verify", WebIdentityToken=token
    )["Credentials"]


def s3_for(creds):
    return boto3.client(
        "s3", endpoint_url=EP, region_name=REGION, config=_CFG,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def main():
    print(f"verify tenant={TENANT} tier={TIER} account={ACCOUNT_ID} endpoint={EP}")
    creds = assume(get_token())
    s3 = s3_for(creds)
    s3.list_objects_v2(Bucket=BUCKET)
    print(f"PASS  list {BUCKET}")
    if TIER in ("admin", "writer"):
        key, body = f"verify-{uuid.uuid4().hex[:8]}.txt", b"ok"
        s3.put_object(Bucket=BUCKET, Key=key, Body=body)
        got = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        s3.delete_object(Bucket=BUCKET, Key=key)
        if got != body:
            print("FAIL  object round-trip mismatch", file=sys.stderr)
            return 1
        print(f"PASS  put/get/delete {key}")
    else:  # reader must be denied a write
        try:
            s3.put_object(Bucket=BUCKET, Key="denied.txt", Body=b"x")
        except ClientError as exc:
            # Narrow, because this is the assertion, not the setup. Catching every
            # ClientError meant a wrong bucket name or a dead endpoint printed PASS.
            if not denied(exc):
                print(f"FAIL  reader write failed for the wrong reason: {exc}", file=sys.stderr)
                return 1
            print("PASS  reader write correctly denied")
        else:
            print("FAIL  reader write was allowed", file=sys.stderr)
            return 1
    if CROSS_BUCKET:
        # The account boundary holds regardless of tier: no reach into another tenant's bucket.
        # This is the check that the whole isolation claim rests on, so it has to fail on
        # NoSuchBucket. Point CROSS_BUCKET at a bucket that does not exist and the old code
        # reported the boundary holding, having proved nothing at all.
        try:
            s3.list_objects_v2(Bucket=CROSS_BUCKET)
        except ClientError as exc:
            if not denied(exc):
                print(
                    f"FAIL  cross-tenant {CROSS_BUCKET} check is inconclusive, not denied: {exc}",
                    file=sys.stderr,
                )
                return 1
            print(f"PASS  cross-tenant {CROSS_BUCKET} correctly denied")
        else:
            print(f"FAIL  cross-tenant {CROSS_BUCKET} was reachable", file=sys.stderr)
            return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
