#!/usr/bin/env python3
"""End-to-end federation smoke test for one tenant role: login -> assume -> scoped S3.

Walks the full path an end user takes:
  OIDC login (ROPC) -> access token carrying the grant claim
    -> sts.AssumeRoleWithWebIdentity(<tenant>-<role>)
      -> temporary creds -> an S3 op the role allows, plus one it does not.

The login user must hold the grant "/services/s3/<TENANT>/<ROLE>" (membership of that group)
in the claim rgw_iam builds its trust conditions from (rgw_iam_grant_claim, default s3iam).
One role is checked per run.
Config via env:

  EP           RGW S3/STS/IAM endpoint (e.g. https://s3.example.com)
  IDP          OIDC token endpoint (.../realms/<realm>/protocol/openid-connect/token)
  CLIENT       OIDC client id (aud), e.g. ceph-s3
  ACCOUNT_ID   the tenant's RGW account id (RGW + 17 digits)
  TENANT       tenant slug
  ROLE         role name within the tenant (reader, writer, owner, iamadmin, ...);
               the IAM role assumed is <TENANT>-<ROLE>
  EXPECT       "rw" (default) if the role may write, "ro" if a write must be denied
  USERNAME     login user (default "<TENANT>-<ROLE>")
  PASSWORD     that user's password
  BUCKET       a bucket to exercise (default "<TENANT>-data")
  CROSS_BUCKET a bucket in another tenant's account (optional). If set, listing it must
               be denied, which tests the account-isolation boundary, not role scoping.
  AWS_REGION   SigV4 signing region, default us-east-1 (RGW ignores it)

Exit 0 = the role's allowed op worked, a denied op was denied,
and (when CROSS_BUCKET is set) another tenant's bucket was out of reach.
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

EP = os.environ["EP"]
IDP = os.environ["IDP"]
CLIENT = os.environ["CLIENT"]
ACCOUNT_ID = os.environ["ACCOUNT_ID"]
TENANT = os.environ["TENANT"]
ROLE = os.environ["ROLE"]
EXPECT = os.environ.get("EXPECT", "rw")
USERNAME = os.environ.get("USERNAME", f"{TENANT}-{ROLE}")
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
    arn = f"arn:aws:iam::{ACCOUNT_ID}:role/{TENANT}-{ROLE}"
    return sts.assume_role_with_web_identity(
        RoleArn=arn, RoleSessionName=f"{TENANT}-{ROLE}-verify", WebIdentityToken=token
    )["Credentials"]


def s3_for(creds):
    return boto3.client(
        "s3", endpoint_url=EP, region_name=REGION, config=_CFG,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )


def denied(exc):
    """True for AccessDenied or a 403; NoSuchBucket or a 5xx is not a denial."""
    error = exc.response.get("Error", {})
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return error.get("Code") == "AccessDenied" or status == 403


def main():
    print(f"verify tenant={TENANT} role={ROLE} expect={EXPECT} account={ACCOUNT_ID} endpoint={EP}")
    creds = assume(get_token())
    s3 = s3_for(creds)
    s3.list_objects_v2(Bucket=BUCKET)
    print(f"PASS  list {BUCKET}")
    if EXPECT == "rw":
        key, body = f"verify-{uuid.uuid4().hex[:8]}.txt", b"ok"
        s3.put_object(Bucket=BUCKET, Key=key, Body=body)
        got = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        s3.delete_object(Bucket=BUCKET, Key=key)
        if got != body:
            print("FAIL  object round-trip mismatch", file=sys.stderr)
            return 1
        print(f"PASS  put/get/delete {key}")
    else:  # a read-only role must be denied a write
        try:
            s3.put_object(Bucket=BUCKET, Key="denied.txt", Body=b"x")
        except ClientError as exc:
            if not denied(exc):
                print(f"FAIL  write failed for another reason: {exc}", file=sys.stderr)
                return 1
            print("PASS  write correctly denied")
        else:
            print("FAIL  write was allowed on a read-only role", file=sys.stderr)
            return 1
    if CROSS_BUCKET:
        # The account boundary must hold whatever the role's capabilities say.
        try:
            s3.list_objects_v2(Bucket=CROSS_BUCKET)
        except ClientError as exc:
            if not denied(exc):
                print(f"FAIL  cross-tenant {CROSS_BUCKET} failed for another reason: {exc}",
                      file=sys.stderr)
                return 1
            print(f"PASS  cross-tenant {CROSS_BUCKET} correctly denied")
        else:
            print(f"FAIL  cross-tenant {CROSS_BUCKET} was reachable", file=sys.stderr)
            return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
