# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""Whether an S3 or IAM refusal means an unknown key or a user without permission.

Squid answers an unknown access key with AccessDenied (a bare 403 on HeadBucket) where AWS
sends InvalidAccessKeyId, and a user without permission gets the same answer.
The boto modules may run where radosgw-admin is missing.
rgw_key_holder_info looks the key up on a Ceph host beforehand,
and its result reaches them as key_holder.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

# InvalidAccessKeyId and SignatureDoesNotMatch are what AWS sends; squid sends the others.
# UserSuspended always names a held key; the lookup then finds the suspended holder.
REFUSED_CODES = ("AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "403",
                 "UserSuspended")

# The fields of rgw_key_holder_info's key_holder result the boto modules read.
# Not a secret; validate-modules flags the name otherwise.
KEY_HOLDER_ARGS = dict(
    key_holder=dict(type="dict", no_log=False, options=dict(
        found=dict(type="bool", required=True),
        user_id=dict(type="str"),
        account_id=dict(type="str"),
        suspended=dict(type="bool", default=False),
    )),
)


def holder_summary(info):
    """The key_holder result for a `user info`, or for None when nobody holds the key.

    Only these fields leave the Ceph host: `user info` also prints the secret keys.
    """
    if info is None:
        return {"found": False}
    return {"found": True, "user_id": info.get("user_id", ""),
            "account_id": info.get("account_id", ""),
            "suspended": bool(info.get("suspended"))}


def error_code(exc):
    """The S3/IAM error code of a ClientError, falling back to the HTTP status."""
    response = getattr(exc, "response", None) or {}
    code = (response.get("Error") or {}).get("Code") or ""
    if not code:
        code = str((response.get("ResponseMetadata") or {}).get("HTTPStatusCode") or "")
    return code


def key_holder(module, what, code):
    """Who holds module's access_key, from O(key_holder), or None when nobody does.

    `what` names the refused call and `code` its error code. Fails the task when no
    key_holder was given, since the refusal cannot be told apart then.
    """
    given = module.params.get("key_holder")
    if given is None:
        module.fail_json(msg="%s was refused (%s). Pass key_holder from "
                             "rgw_key_holder_info to tell a key no user holds from a user "
                             "without permission." % (what, code))
    if not given["found"]:
        return None
    return given


def refused_msg(what, code, holder):
    """The failure message for a refusal that is not a missing prerequisite."""
    if holder is None:
        return ("%s was refused (%s): no RGW user holds this access key. Create the "
                "account-root user with these keys first." % (what, code))
    return ("%s was refused (%s), although user '%s' in account '%s' holds this access "
            "key. Check its secret key, its permissions and whether it is suspended."
            % (what, code, holder.get("user_id", "?"), holder.get("account_id", "?")))


def exit_if_suspended_preview(module, holder, what, **result):
    """In check mode, exit unprobed when the key's holder exists but is suspended.

    RGW refuses a suspended user's keys like unknown ones. In a role run an earlier task
    enables the account-root user, which --check only reports, so this step cannot read
    the current state. A real run still fails here.
    """
    if not (module.check_mode and holder is not None and holder.get("suspended")):
        return
    msg = ("not probed: %s was refused because user '%s', which holds this access key, "
           "is suspended. Enable the user to preview this step."
           % (what, holder.get("user_id", "?")))
    module.warn(msg)
    module.exit_json(changed=False, probed=False, msg=msg, **result)
