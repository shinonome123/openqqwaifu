"""Run the tests in an empty working directory, never in the repository.

Many tests build services from a default ``AppConfig()``, whose ``data_root`` is the
relative "data". Run from the repository, that is the live bot's data: on 2026-10-28 a
test run wrote its messages into a tavern game someone was playing there. Discovery
(``unittest discover -s tests``) imports this module first, since its name sorts first;
running single modules (``python -m unittest tests.test_app``) reaches it through
``tests/__init__.py``.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import unittest
from pathlib import Path

_MARK = "OPENQQWAIFU_TEST_WORKDIR"
REPOSITORY = Path(__file__).resolve().parents[1]

if not os.environ.get(_MARK):
    _workdir = tempfile.mkdtemp(prefix="openqqwaifu-tests-")
    _previous = os.getcwd()
    os.chdir(_workdir)
    os.environ[_MARK] = _workdir

    def _restore() -> None:
        os.chdir(_previous)
        shutil.rmtree(_workdir, ignore_errors=True)

    atexit.register(_restore)


class IsolatedWorkingDirectoryTests(unittest.TestCase):
    def test_tests_do_not_run_inside_the_repository(self) -> None:
        cwd = Path.cwd().resolve()
        self.assertNotEqual(cwd, REPOSITORY)
        self.assertFalse((cwd / "data").exists() and cwd.is_relative_to(REPOSITORY))


if __name__ == "__main__":
    unittest.main()
