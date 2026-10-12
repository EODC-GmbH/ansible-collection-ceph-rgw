#!/usr/bin/env python3
"""Federation probes for scenarios.sh that verify_federation.py does not cover.

    fed_probe.py seed <bucket> <key>...          put empty objects with AK/SK (a root key)
    fed_probe.py unseed <bucket> <key>...        delete them again with AK/SK
    fed_probe.py denied                          assuming <TENANT>-<ROLE> must be refused
    fed_probe.py prefix <bucket> <in> <out>      the role reads under prefix <in> only;
                                                 <in>probe and <out>probe must exist

Environment as verify_federation.py: EP, IDP, CLIENT, ACCOUNT_ID, TENANT, ROLE, USERNAME,
PASSWORD (and AK/SK for seed and unseed). Runs where EP and IDP are reachable and boto3 is
installed.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ENV = os.environ
CFG = Config(signature_version="s3v4", s3={"addressing_style": "path"})
REGION = ENV.get("AWS_REGION", "us-east-1")


def s3_client(ak, sk, token=None):
    return boto3.client("s3", endpoint_url=ENV["EP"], region_name=REGION, config=CFG,
                        aws_access_key_id=ak, aws_secret_access_key=sk, aws_session_token=token)


def login():
    data = urllib.parse.urlencode({
        "grant_type": "password", "client_id": ENV["CLIENT"], "scope": "openid",
        "username": ENV["USERNAME"], "password": ENV["PASSWORD"],
    }).encode()
    with urllib.request.urlopen(ENV["IDP"], data) as r:  # noqa: S310 - operator-supplied IdP URL
        return json.load(r)["access_token"]


def assume(token):
    sts = boto3.client("sts", endpoint_url=ENV["EP"], region_name=REGION)
    arn = f"arn:aws:iam::{ENV['ACCOUNT_ID']}:role/{ENV['TENANT']}-{ENV['ROLE']}"
    return sts.assume_role_with_web_identity(
        RoleArn=arn, RoleSessionName="scenario-probe", WebIdentityToken=token)["Credentials"]


def denied(exc):
    error = exc.response.get("Error", {})
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return error.get("Code") == "AccessDenied" or status == 403


def expect_denied(what, call):
    try:
        call()
    except ClientError as exc:
        if not denied(exc):
            sys.exit(f"FAIL  {what} failed for another reason: {exc}")
        print(f"PASS  {what} denied")
        return
    sys.exit(f"FAIL  {what} was allowed")


def seed(bucket, *keys):
    s3 = s3_client(ENV["AK"], ENV["SK"])
    for key in keys:
        s3.put_object(Bucket=bucket, Key=key, Body=b"")
    print(f"PASS  seeded {len(keys)} objects in {bucket}")


def unseed(bucket, *keys):
    s3 = s3_client(ENV["AK"], ENV["SK"])
    for key in keys:
        s3.delete_object(Bucket=bucket, Key=key)
    print(f"PASS  removed {len(keys)} objects from {bucket}")


def assume_denied():
    token = login()  # a login failure is a broken setup, not a denial
    expect_denied(f"{ENV['USERNAME']} assuming {ENV['TENANT']}-{ENV['ROLE']}", lambda: assume(token))


def prefix(bucket, inside, outside):
    c = assume(login())
    s3 = s3_client(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"])
    listed = [o["Key"] for o in s3.list_objects_v2(Bucket=bucket, Prefix=inside).get("Contents", [])]
    if not listed:
        sys.exit(f"FAIL  nothing listed under {inside}")
    s3.get_object(Bucket=bucket, Key=listed[0])
    print(f"PASS  list and get under {inside}")
    expect_denied(f"list under {outside}", lambda: s3.list_objects_v2(Bucket=bucket, Prefix=outside))
    expect_denied("list of the whole bucket", lambda: s3.list_objects_v2(Bucket=bucket))
    expect_denied(f"get {outside}probe", lambda: s3.get_object(Bucket=bucket, Key=f"{outside}probe"))


if __name__ == "__main__":
    command, args = sys.argv[1], sys.argv[2:]
    {"seed": seed, "unseed": unseed, "denied": assume_denied, "prefix": prefix}[command](*args)
