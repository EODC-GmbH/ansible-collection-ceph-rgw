# -*- coding: utf-8 -*-
# Copyright: (c) 2026, Underknowledge
# Apache-2.0
"""patch_module_args for every ansible-core the collection supports.

ansible-core 2.19 added ansible.module_utils.testing.patch_module_args; older cores
(kayobe 2025.1 ships 2.18) read the module's arguments from basic._ANSIBLE_ARGS.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

try:
    from ansible.module_utils.testing import patch_module_args  # noqa: F401
except ImportError:
    import contextlib
    import json

    from ansible.module_utils import basic
    from ansible.module_utils.common.text.converters import to_bytes

    @contextlib.contextmanager
    def patch_module_args(args=None):
        saved = basic._ANSIBLE_ARGS
        basic._ANSIBLE_ARGS = to_bytes(json.dumps({"ANSIBLE_MODULE_ARGS": args or {}}))
        try:
            yield
        finally:
            basic._ANSIBLE_ARGS = saved
