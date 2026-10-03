"""Offline test helpers for clocks shared by the extracted modules."""

from contextlib import ExitStack
from unittest.mock import patch

from libcs import booking, client, configuration, operations, records, scheduling


class RuntimeDatetimePatch:
    """Patch the same virtual clock into every module that reads wall time."""

    def __init__(self, value):
        self.value = value
        self.stack = ExitStack()

    def start(self):
        for module in (booking, client, configuration, operations, records, scheduling):
            if hasattr(module, "datetime"):
                self.stack.enter_context(patch.object(module, "datetime", self.value))
        return self.value

    def stop(self):
        self.stack.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)


def patch_runtime_datetime(value):
    return RuntimeDatetimePatch(value)


def patch_runtime(target, value):
    if target == "datetime":
        return patch_runtime_datetime(value)
    if target.startswith("time."):
        return patch(target, value)
    raise ValueError(f"Unexpected clock test target: {target}")
