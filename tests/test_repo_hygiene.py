from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False)


@unittest.skipIf(shutil.which("git") is None, "git is not installed")
class RepoHygieneTests(unittest.TestCase):
    def setUp(self) -> None:
        if _git("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            self.skipTest("not a git work tree")

    def test_local_secrets_and_scratch_output_are_ignored(self) -> None:
        # --no-index: also holds when such a file was committed by mistake.
        for path in (".env", ".env.production", "output/screenshot.png"):
            self.assertEqual(_git("check-ignore", "-q", "--no-index", path).returncode, 0, path)

    def test_env_example_stays_tracked(self) -> None:
        self.assertEqual(_git("check-ignore", "-q", "--no-index", ".env.example").returncode, 1)


if __name__ == "__main__":
    unittest.main()
