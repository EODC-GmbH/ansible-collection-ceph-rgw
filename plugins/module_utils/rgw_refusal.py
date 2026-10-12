# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""Whether an S3 or IAM refusal means an unknown key or a user without permission.

Squid answers an unknown access key with AccessDenied (a bare 403 on HeadBucket)
where AWS sends InvalidAccessKeyId,
and a user without permission gets the same answer.
After a refusal the boto modules ask `radosgw-admin user info --access-key` which it was.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw import RGWError
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw_read import (
    read_key_holder,
)

# InvalidAccessKeyId and SignatureDoesNotMatch are what AWS sends; squid sends the others.
# UserSuspended always names a held key; the lookup then finds the suspended holder.
REFUSED_CODES = ("AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "403",
                 "UserSuspended")


def error_code(exc):
    """The S3/IAM error code of a ClientError, falling back to the HTTP status."""
    response = getattr(exc, "response", None) or {}
    code = (response.get("Error") or {}).get("Code") or ""
    if not code:
        code = str((response.get("ResponseMetadata") or {}).get("HTTPStatusCode") or "")
    return code


def key_holder(module, rgw, what):
    """Who holds module's access_key, as `user info`, or None when nobody does.

    `what` names the refused call.
    Fails the task when the lookup cannot reach the cluster.
    """
    try:
        return read_key_holder(rgw, module.params["access_key"])
    except RGWError as exc:
        module.fail_json(msg="%s was refused, and radosgw-admin could not tell whether any "
                             "user holds the access key: %s" % (what, exc.msg),
                         rc=exc.rc, stderr=exc.stderr, cmd=exc.cmd)


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

    RGW refuses a suspended user's keys like unknown ones. In a role run the
    account-root user is enabled by an earlier task, which --check only reports,
    so this step cannot read the current state. A real run still fails here.
    """
    if not (module.check_mode and holder is not None and holder.get("suspended")):
        return
    msg = ("not probed: %s was refused because user '%s', which holds this access key, "
           "is suspended. Enable the user to preview this step."
           % (what, holder.get("user_id", "?")))
    module.warn(msg)
    module.exit_json(changed=False, probed=False, msg=msg, **result)
