# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""rgw_orch_restart returns only once the service's daemons are running again.

`ceph orch restart` is asynchronous and `ceph orch ps` answers from cephadm's cache, so right
after the request the old daemon still shows as running. The fake shows the restart only after
some polls.

Run:  python3 -m unittest discover -s tests/unit
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

import contextlib
import json
import unittest
from unittest import mock

from test_rgw_oidc_replace import _bind_fqcn

_bind_fqcn()

from ansible.module_utils import basic  # noqa: E402
from module_args import patch_module_args  # noqa: E402
from ansible_collections.underknowledge.ceph_rgw.plugins.module_utils.rgw import RGW  # noqa: E402

import rgw_orch_restart  # noqa: E402

OLD = "2026-10-05T14:25:46.579509Z"
NEW = "2026-10-05T15:02:11.104223Z"


def daemon(started, container_id, status_desc="running"):
    # The fields of a real squid `ceph orch ps --format json` row the module reads.
    return {"daemon_name": "rgw.rgw.host1.abcdef", "service_name": "rgw.rgw",
            "status_desc": status_desc, "started": started, "container_id": container_id}


class Orch(object):
    """`ceph orch ps` and `ceph orch restart` for one daemon.

    views is what successive `orch ps --refresh` calls return after the restart; the last one
    repeats.
    """

    def __init__(self, views):
        self.current = [daemon(OLD, "ad5909a84852")]
        self.views = list(views)
        self.calls = []

    def handle(self, argv):
        self.calls.append(tuple(argv))
        if argv[:2] == ["orch", "restart"]:
            return 0, "Scheduled to restart rgw.rgw.host1.abcdef on host 'host1'\n", ""
        if argv[:2] == ["orch", "ps"]:
            if "--refresh" in argv and self.views:
                self.current = self.views.pop(0) if len(self.views) > 1 else self.views[0]
            # cephadm shell leaves a blank line ahead of the JSON.
            return 0, "\n" + json.dumps(self.current) + "\n", ""
        raise AssertionError("fake does not model ceph %s" % (argv,))

    def restarts(self):
        return [c for c in self.calls if c[:2] == ("orch", "restart")]


class OrchRGW(RGW):
    orch = None

    def _resolve(self):
        self._base, self._mode = [], "native"

    def _run_raw(self, argv):
        return OrchRGW.orch.handle([str(a) for a in argv])


class Exited(Exception):
    def __init__(self, failed, kwargs):
        super(Exited, self).__init__(kwargs.get("msg"))
        self.failed = failed
        self.kwargs = kwargs


class Clock(object):
    """time.monotonic that only advances when the module sleeps."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run(orch, check_mode=False, **params):
    args = dict(dict(service="rgw.rgw"), **params)
    args.update(_ansible_check_mode=check_mode)
    clock = Clock()

    def leave(failed):
        def _exit(self, **kwargs):
            raise Exited(failed, kwargs)
        return _exit

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch_module_args(args))
        stack.enter_context(mock.patch.object(OrchRGW, "orch", orch))
        stack.enter_context(mock.patch.object(rgw_orch_restart, "RGW", OrchRGW))
        stack.enter_context(mock.patch.object(rgw_orch_restart, "time", clock))
        stack.enter_context(mock.patch.object(basic.AnsibleModule, "exit_json", leave(False)))
        stack.enter_context(mock.patch.object(basic.AnsibleModule, "fail_json", leave(True)))
        try:
            rgw_orch_restart.main()
        except Exited as exc:
            return exc, clock
    raise AssertionError("module returned without exit_json")


STALE = [daemon(OLD, "ad5909a84852")]
STOPPED = [daemon(OLD, "ad5909a84852", "stopped")]
STARTING = [daemon(NEW, "5b1c0e7d9a40", "starting")]
BACK = [daemon(NEW, "5b1c0e7d9a40")]


class OrchRestartTest(unittest.TestCase):

    def test_waits_until_the_daemon_restarted(self):
        orch = Orch([STALE, STOPPED, STARTING, BACK])
        result, clock = run(orch)
        self.assertFalse(result.failed, result.kwargs.get("msg"))
        self.assertTrue(result.kwargs["changed"])
        self.assertEqual(result.kwargs["daemons"],
                         [{"daemon_name": "rgw.rgw.host1.abcdef", "started": NEW}])
        self.assertEqual(len(orch.restarts()), 1)
        self.assertEqual(clock.now, 3 * rgw_orch_restart.POLL_INTERVAL)

    def test_snapshot_is_taken_before_the_restart(self):
        orch = Orch([BACK])
        run(orch)
        verbs = [c[:2] for c in orch.calls]
        self.assertEqual(verbs[:2], [("orch", "ps"), ("orch", "restart")])
        self.assertNotIn("--refresh", orch.calls[0])
        self.assertIn("--refresh", orch.calls[2])

    def test_stale_running_is_not_taken_as_restarted(self):
        orch = Orch([STALE])
        result, clock = run(orch, wait_timeout=60)
        self.assertTrue(result.failed)
        self.assertIn("did not come back within 60s", result.kwargs["msg"])
        self.assertIn("has not restarted yet (started %s)" % OLD, result.kwargs["msg"])
        self.assertEqual(clock.now, 60)

    def test_a_new_container_with_the_same_start_time_counts(self):
        orch = Orch([[daemon(OLD, "5b1c0e7d9a40")]])
        result, _ = run(orch)
        self.assertFalse(result.failed, result.kwargs.get("msg"))

    def test_timeout_names_a_daemon_that_never_runs(self):
        orch = Orch([STOPPED, [daemon(NEW, "5b1c0e7d9a40", "error")]])
        result, _ = run(orch, wait_timeout=12)
        self.assertTrue(result.failed)
        self.assertIn("rgw.rgw.host1.abcdef is error", result.kwargs["msg"])

    def test_wait_disabled(self):
        orch = Orch([STALE])
        result, clock = run(orch, wait_timeout=0)
        self.assertFalse(result.failed)
        self.assertEqual(orch.calls, [("orch", "restart", "rgw.rgw")])
        self.assertEqual(result.kwargs["daemons"], [])
        self.assertEqual(clock.now, 0)

    def test_check_mode_does_nothing(self):
        orch = Orch([BACK])
        result, _ = run(orch, check_mode=True)
        self.assertFalse(result.failed)
        self.assertTrue(result.kwargs["changed"])
        self.assertEqual(orch.calls, [])


if __name__ == "__main__":
    unittest.main()
