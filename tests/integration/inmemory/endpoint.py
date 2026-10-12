# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""An S3/IAM endpoint over the in-memory cluster, for boto3 in the collection's modules.

    python3 endpoint.py <port file>

Listens on 127.0.0.1 (path-style S3) on a free port and writes the port to <port file>. Every
request loads FAKE_CEPH_STATE, is answered by fake_aws.FakeAWS, and saves it again, so refusals
such as an unknown key or a suspended user follow the cluster. Signatures are not checked; the
access key is read from the Authorization header. Calls the collection does not make answer 501,
so a new one shows up as a failure. Needs a Python with ansible-core and botocore.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from xml.sax.saxutils import escape

from botocore.exceptions import ClientError  # before fake_aws, so it is not stubbed

from state import opened

from fake_aws import FakeAWS, client_error  # noqa: E402  (state.py puts tests/unit on the path)

CREDENTIAL = re.compile(r"Credential=([^/]+)/[^/]+/[^/]+/([^/]+)/")
STATE = ("buckets", "foreign_buckets", "providers", "user_policies")
IAM_NS = "https://iam.amazonaws.com/doc/2010-05-08/"


def members(name, values):
    return "<%s>%s</%s>" % (name, "".join("<member>%s</member>" % escape(v)
                                          for v in values), name)


def listed(params, name):
    """Query-protocol list parameters: Name.member.1, Name.member.2, ..."""
    found = sorted((int(k.rsplit(".", 1)[1]), v[0]) for k, v in params.items()
                   if k.startswith(name + ".member."))
    return [v for _, v in found]


def s3(aws, method, path, query):
    """(status, body) for the S3 calls rgw_bucket makes."""
    bucket = path.strip("/")
    if query or "/" in bucket:
        raise client_error("NotImplemented", 501)
    if method == "GET" and not bucket:
        names = aws.list_buckets()["Buckets"]
        return 200, ("<ListAllMyBucketsResult><Owner><ID>fake</ID></Owner><Buckets>%s"
                     "</Buckets></ListAllMyBucketsResult>"
                     % "".join("<Bucket><Name>%s</Name><CreationDate>2026-10-08T00:00:00Z"
                               "</CreationDate></Bucket>" % escape(b["Name"]) for b in names))
    calls = {"HEAD": (aws.head_bucket, 200), "PUT": (aws.create_bucket, 200),
             "DELETE": (aws.delete_bucket, 204)}
    if not bucket or method not in calls:
        raise client_error("NotImplemented", 501)
    call, status = calls[method]
    call(Bucket=bucket)
    return status, ""


def iam(aws, params):
    """(status, body) for the IAM calls rgw_oidc_provider and rgw_user_policy make."""
    action = params.get("Action", [""])[0]
    one = {k: v[0] for k, v in params.items()}
    if action == "ListOpenIDConnectProviders":
        arns = [p["Arn"] for p in aws.list_open_id_connect_providers()
                ["OpenIDConnectProviderList"]]
        result = "<OpenIDConnectProviderList>%s</OpenIDConnectProviderList>" % "".join(
            "<member><Arn>%s</Arn></member>" % escape(a) for a in arns)
    elif action == "GetOpenIDConnectProvider":
        arn = one["OpenIDConnectProviderArn"]
        got = aws.get_open_id_connect_provider(OpenIDConnectProviderArn=arn)
        result = (members("ClientIDList", got["ClientIDList"])
                  + members("ThumbprintList", got["ThumbprintList"])
                  + "<Url>%s</Url>" % escape(arn.split(":oidc-provider/", 1)[1]))
    elif action == "CreateOpenIDConnectProvider":
        arn = aws.create_open_id_connect_provider(
            Url=one["Url"], ClientIDList=listed(params, "ClientIDList"),
            ThumbprintList=listed(params, "ThumbprintList"))["OpenIDConnectProviderArn"]
        result = "<OpenIDConnectProviderArn>%s</OpenIDConnectProviderArn>" % escape(arn)
    elif action == "DeleteOpenIDConnectProvider":
        aws.delete_open_id_connect_provider(
            OpenIDConnectProviderArn=one["OpenIDConnectProviderArn"])
        result = ""
    elif action == "ListUserPolicies":
        names = aws.list_user_policies(UserName=one["UserName"])["PolicyNames"]
        result = members("PolicyNames", names) + "<IsTruncated>false</IsTruncated>"
    elif action == "GetUserPolicy":
        doc = aws.get_user_policy(UserName=one["UserName"],
                                  PolicyName=one["PolicyName"])["PolicyDocument"]
        result = ("<UserName>%s</UserName><PolicyName>%s</PolicyName>"
                  "<PolicyDocument>%s</PolicyDocument>"
                  % (escape(one["UserName"]), escape(one["PolicyName"]), escape(doc)))
    elif action == "PutUserPolicy":
        aws.put_user_policy(UserName=one["UserName"], PolicyName=one["PolicyName"],
                            PolicyDocument=one["PolicyDocument"])
        result = ""
    elif action == "DeleteUserPolicy":
        aws.delete_user_policy(UserName=one["UserName"], PolicyName=one["PolicyName"])
        result = ""
    else:
        raise client_error("NotImplemented", 501)
    return 200, ('<{0}Response xmlns="{1}"><{0}Result>{2}</{0}Result>'
                 '<ResponseMetadata><RequestId>fake</RequestId></ResponseMetadata>'
                 '</{0}Response>'.format(action, IAM_NS, result))


class Handler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"

    def answer(self, status, body):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(0 if self.command == "HEAD" else len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def handle_one(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        signed = CREDENTIAL.search(self.headers.get("Authorization") or "")
        if not signed:
            return self.answer(403, "<Error><Code>AccessDenied</Code></Error>")
        access_key, service = signed.groups()
        url = urlsplit(self.path)
        with opened() as (cluster, saved):
            aws = FakeAWS(cluster)
            for name in STATE:
                setattr(aws, name, getattr(saved, name))
            aws.access_key = access_key
            try:
                if service == "iam":
                    status, out = iam(aws, parse_qs(body))
                else:
                    status, out = s3(aws, self.command, url.path, url.query)
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                status = exc.response["ResponseMetadata"]["HTTPStatusCode"]
                out = ("<ErrorResponse><Error><Type>Sender</Type><Code>%s</Code>"
                       "<Message>%s</Message></Error></ErrorResponse>" % (code, code)
                       if service == "iam" else "<Error><Code>%s</Code></Error>" % code)
        self.answer(status, out)

    do_GET = do_HEAD = do_PUT = do_DELETE = do_POST = handle_one

    def log_message(self, fmt, *args):
        sys.stderr.write("endpoint: %s\n" % (fmt % args))


def main(port_file):
    # Threads, because boto3 keeps connections open; the state file lock serializes them.
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    with open(port_file, "w") as handle:
        handle.write(str(server.server_address[1]))
    server.serve_forever()


if __name__ == "__main__":
    main(sys.argv[1])
