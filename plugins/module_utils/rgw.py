# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""Shared execution layer: locates radosgw-admin (or ceph) and runs it with an argv list."""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import json
import os
import re

CEPH_CONF = "/etc/ceph/ceph.conf"
_FSID_RE = re.compile(r"^\s*fsid\s*=\s*([0-9a-f-]{36})\s*$", re.MULTILINE)

RGW_COMMON_ARGS = dict(
    exec_mode=dict(type="str", default="auto",
                   choices=["auto", "native", "container", "cephadm"]),
    fsid=dict(type="str"),
    image=dict(type="str"),
    container_runtime=dict(type="str", choices=["docker", "podman"]),
    cluster=dict(type="str", default="ceph"),
    # A path, not a secret; validate-modules flags the name otherwise. In container mode
    # only /etc/ceph is mounted, so the keyring must live there.
    keyring=dict(type="str", no_log=False),
)


class RGWError(Exception):
    """A radosgw-admin invocation that the calling module should surface as failed."""

    def __init__(self, msg, rc=None, stderr=None, cmd=None):
        super(RGWError, self).__init__(msg)
        self.msg = msg
        self.rc = rc
        self.stderr = stderr
        self.cmd = cmd


class RGW(object):
    """Runs radosgw-admin/ceph against the local cluster, however it is reachable.

    Resolution order under exec_mode=auto:
      native    - the binary is on PATH (non-containerised clusters)
      container - `<runtime> run --rm --net=host -v /etc/ceph:/etc/ceph:ro
                  --entrypoint <binary> <image>`, ~0.4s
      cephadm   - `cephadm shell --fsid <fsid> -- <binary>`, ~13s, always correct

    cephadm shell pays a full Python startup and image inspection on every call, so the
    container path is tried first. exec_mode_used reports which path actually ran.
    """

    def __init__(self, module, binary="radosgw-admin"):
        self.module = module
        self.binary = binary
        self._base = None
        self._mode = None
        self._fsid = None

    def fsid(self):
        """The cluster fsid, from ceph.conf rather than a 13s `cephadm shell -- ceph fsid`."""
        if self._fsid:
            return self._fsid
        given = self.module.params.get("fsid")
        if given:
            self._fsid = given
            return self._fsid
        try:
            with open(CEPH_CONF) as handle:
                found = _FSID_RE.search(handle.read())
        except (IOError, OSError):
            found = None
        if found:
            self._fsid = found.group(1)
            return self._fsid
        rc, out, err = self._run_raw(["cephadm", "shell", "--", "ceph", "fsid"])
        if rc != 0:
            raise RGWError("could not determine the cluster fsid: %s" % (err or out).strip())
        self._fsid = out.strip().splitlines()[-1].strip()
        return self._fsid

    def _runtime(self):
        given = self.module.params.get("container_runtime")
        candidates = [given] if given else ["podman", "docker"]
        for name in candidates:
            path = self.module.get_bin_path(name)
            if path:
                return path
        return None

    def _image(self, runtime):
        """A digest-pinned image reference, taken from a running ceph container."""
        given = self.module.params.get("image")
        if given:
            return given
        env = os.environ.get("CEPH_CONTAINER_IMAGE")
        if env:
            return env
        rc, out, _ = self._run_raw([runtime, "ps", "--filter", "name=ceph-",
                                    "--format", "{{.ID}}"])
        if rc != 0:
            return None
        for cid in out.split():
            rc, img, _ = self._run_raw([runtime, "inspect", "--format",
                                        "{{.Config.Image}}", cid])
            if rc == 0 and "ceph" in img:
                return img.strip()
        return None

    def _run_raw(self, argv):
        # data='\n', not '': run_command only pipes stdin when data is truthy, and an
        # inherited stdin is consumed by `cephadm shell`.
        return self.module.run_command(argv, data="\n", binary_data=False)

    def _resolve(self):
        """Pick how to reach the binary, once per module run."""
        if self._base is not None:
            return
        mode = self.module.params.get("exec_mode") or "auto"

        if mode in ("auto", "native"):
            path = self.module.get_bin_path(self.binary)
            if path:
                self._base, self._mode = [path], "native"
                self._append_global_args()
                return
            if mode == "native":
                raise RGWError("exec_mode=native but %s is not on PATH" % self.binary)

        if mode in ("auto", "container"):
            runtime = self._runtime()
            image = self._image(runtime) if runtime else None
            if runtime and image:
                self._base = [runtime, "run", "--rm", "--net=host",
                              "-v", "/etc/ceph:/etc/ceph:ro",
                              "--entrypoint", self.binary, image]
                self._mode = "container"
                self._append_global_args()
                return
            if mode == "container":
                raise RGWError("exec_mode=container but no %s" %
                               ("container runtime was found" if not runtime
                                else "ceph image could be resolved"))

        cephadm = self.module.get_bin_path("cephadm")
        if not cephadm:
            raise RGWError("no way to reach the cluster: %s is not on PATH, no container "
                           "runtime with a ceph image, and cephadm is missing"
                           % self.binary)
        self._base = [cephadm, "shell", "--fsid", self.fsid(), "--", self.binary]
        self._mode = "cephadm"
        self._append_global_args()

    def _append_global_args(self):
        """--cluster and --keyring, when they are not the defaults."""
        cluster = self.module.params.get("cluster")
        if cluster and cluster != "ceph":
            self._base += ["--cluster", cluster]
        keyring = self.module.params.get("keyring")
        if keyring:
            self._base += ["--keyring", keyring]

    @property
    def exec_mode_used(self):
        return self._mode

    def run(self, *args, **kwargs):
        """Run the binary with these arguments.

        Returns (rc, parsed, stdout, stderr); parsed is the decoded JSON when the output
        is JSON and json_out is true, else None. Raises RGWError when check_rc and rc!=0.
        """
        json_out = kwargs.pop("json_out", True)
        check_rc = kwargs.pop("check_rc", True)
        self._resolve()
        argv = list(self._base) + [str(a) for a in args]
        rc, out, err = self._run_raw(argv)
        if rc != 0 and check_rc:
            raise RGWError(self.redact((err or out).strip()) or
                           "%s exited %d with no output" % (self.binary, rc),
                           rc=rc, stderr=self.redact(err), cmd=self.redact_argv(argv))
        parsed = None
        if json_out and out.strip():
            try:
                parsed = json.loads(out)
            except ValueError:
                parsed = None
        return rc, parsed, out, err

    def redact(self, text):
        """Remove every no_log value from text, such as an argv echoed back in `cmd`."""
        if not text:
            return text
        for secret in self.module.no_log_values:
            if secret:
                text = text.replace(secret, "VALUE_SPECIFIED_IN_NO_LOG_PARAMETER")
        return text

    def redact_argv(self, argv):
        return [self.redact(a) for a in argv]


def config_get(ceph, who, option):
    """The stored value of one ceph config option.

    `cephadm shell` prefixes stdout with its own inferences, so the value is the last
    non-empty line.
    """
    _, _, out, _ = ceph.run("config", "get", who, option, json_out=False)
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def diff_of(before, after):
    return {
        "before": before,
        "after": after,
        "before_header": "current",
        "after_header": "desired",
    }
