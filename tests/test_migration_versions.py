"""Regression tests for Supabase migration filename invariants."""

from collections import defaultdict
from pathlib import Path
import re
import unittest


MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "supabase" / "migrations"
VERSION_PATTERN = re.compile(r"^(\d+)_.*\.sql$")


class MigrationVersionTests(unittest.TestCase):
    def test_numeric_versions_are_unique(self):
        migrations_by_version: dict[int, list[str]] = defaultdict(list)

        for migration in MIGRATIONS_DIR.glob("*.sql"):
            match = VERSION_PATTERN.fullmatch(migration.name)
            if match:
                migrations_by_version[int(match.group(1))].append(migration.name)

        duplicates = {
            version: sorted(filenames)
            for version, filenames in migrations_by_version.items()
            if len(filenames) > 1
        }
        self.assertEqual(
            {},
            duplicates,
            f"duplicate Supabase migration versions: {duplicates}",
        )


if __name__ == "__main__":
    unittest.main()
