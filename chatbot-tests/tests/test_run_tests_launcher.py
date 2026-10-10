"""Regression tests for the test-suite launcher."""

from __future__ import annotations

import sys

import run_tests


def test_pytest_command_disables_socket_binding_rerun_plugin() -> None:
    command = run_tests.build_pytest_command(["--collect-only"])

    assert command[:3] == [sys.executable, "-m", "pytest"]
    assert command[3:5] == ["-p", "no:rerunfailures"]
    assert command[-1] == "--collect-only"
