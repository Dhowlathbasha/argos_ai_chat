"""Compatibility entrypoint for the canonical evidence-enabled scenario suite.

Use ``tests/test_scenario.py`` or ``run_tests.py`` for new commands. Keeping
this module as a thin re-export means older commands still get Playwright
screenshots, video recording, judging, and PDF/JSON evidence.
"""

from tests.test_scenario import discover_scenarios, test_scenario

__all__ = ["discover_scenarios", "test_scenario"]
