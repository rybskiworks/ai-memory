#!/usr/bin/env python3
"""Exercise frozen release prose checks in disposable Git repositories."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


GATE = Path(__file__).with_name("check-changelog-frozen.sh")
BASE = """# Changelog

## [Unreleased]

### Fixed
- Pending fix.

## [2.1.1] - 2026-09-01

### Fixed
- Preserved the current release entry.

## [2.1.0] - 2026-08-01

### Added
- Preserved the older release entry.

[Unreleased]: https://example.org/compare/v2.1.1...HEAD
[2.1.1]: https://example.org/releases/v2.1.1
[2.1.0]: https://example.org/releases/v2.1.0
"""


class FrozenChangelogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="changelog-frozen-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_") and key != "SSH_AUTH_SOCK"
        }
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        self.git("init", "--quiet", "--template=", str(self.repo))
        self.git("config", "user.name", "Changelog Fixture")
        self.git("config", "user.email", "fixture@example.org")
        (self.repo / "scripts").mkdir()
        shutil.copyfile(GATE, self.repo / "scripts" / GATE.name)

    def git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()

    def commit(self, changelog):
        (self.repo / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
        self.git("add", "CHANGELOG.md", "scripts")
        self.git("commit", "--quiet", "--allow-empty", "--no-gpg-sign", "-m", "test: release history")
        return self.git("rev-parse", "HEAD")

    def check_change(self, current, accepted, base=BASE):
        base_sha = self.commit(base)
        self.commit(current)
        result = subprocess.run(
            ["bash", "scripts/check-changelog-frozen.sh", base_sha],
            cwd=self.repo,
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        if accepted:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("modifies an already-released", result.stderr)

    def test_unchanged_releases_pass(self):
        self.check_change(BASE, True)

    def test_unreleased_prose_can_change(self):
        self.check_change(BASE.replace("Pending fix.", "Another pending fix."), True)

    def test_trailing_reference_urls_can_change(self):
        self.check_change(BASE.replace("compare/v2.1.1...HEAD", "compare/v2.4.0...HEAD"), True)

    def test_newer_releases_and_reference_links_can_be_added(self):
        newer = """## [2.4.0] - 2026-09-20

### Added
- New release feature.

## [2.3.0] - 2026-09-15

### Fixed
- New release fix.

"""
        current = BASE.replace("## [2.1.1]", newer + "## [2.1.1]", 1)
        current = current.replace("compare/v2.1.1...HEAD", "compare/v2.4.0...HEAD")
        current += "\n[2.4.0]: https://example.org/releases/v2.4.0\n"
        current += "[2.3.0]: https://example.org/releases/v2.3.0\n"
        self.check_change(current, True)

    def test_current_release_prose_is_frozen(self):
        self.check_change(BASE.replace("current release entry", "rewritten entry"), False)

    def test_older_release_prose_is_frozen(self):
        self.check_change(BASE.replace("older release entry", "rewritten entry"), False)

    def test_footer_update_does_not_mask_historical_prose_change(self):
        current = BASE.replace("compare/v2.1.1...HEAD", "compare/v2.4.0...HEAD")
        self.check_change(current.replace("older release entry", "rewritten entry"), False)

    def test_reference_definitions_followed_by_prose_are_not_a_footer(self):
        base = BASE.replace(
            "[Unreleased]:",
            "[detail]: https://example.org/old\n\nHistorical explanation.\n\n[Unreleased]:",
        )
        self.check_change(base.replace("example.org/old", "example.org/new"), False, base)

    def test_prose_after_reference_definitions_is_still_frozen(self):
        base = BASE + "\nHistorical explanation after links.\n"
        self.check_change(base.replace("Historical explanation", "Changed explanation"), False, base)

    def test_prose_appended_after_footer_is_rejected(self):
        self.check_change(BASE + "\nAn extra historical claim.\n", False)


if __name__ == "__main__":
    unittest.main()
