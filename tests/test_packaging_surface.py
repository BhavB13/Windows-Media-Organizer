"""What the installable package ships, and what it must not.

These are packaging assertions rather than behaviour tests: they fail when a
module or dependency that was deliberately removed creeps back into the
distribution metadata.
"""

import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


class LegacyFrontendIsNotShippedTests(unittest.TestCase):
    """The Tkinter frontend moved duplicates with no manifest and no restore.

    It contradicted the product's stated "nothing is deleted" invariant while
    README documented it as a supported launch, so it was removed. These
    assertions stop it, or the dependency that existed only to serve it,
    returning to the distribution.
    """

    def test_the_legacy_modules_are_gone_from_the_repository(self):
        for name in ("legacy_main.py", "ui_components.py"):
            self.assertFalse(
                (REPO_ROOT / name).exists(),
                f"{name} moved duplicate originals with no recovery record",
            )

    def test_the_legacy_modules_are_not_declared_as_py_modules(self):
        pyproject = _read("pyproject.toml")
        modules = re.search(r"py-modules\s*=\s*\[(.*?)\]", pyproject, re.S)
        self.assertIsNotNone(modules, "py-modules block not found in pyproject.toml")
        declared = modules.group(1)
        for name in ("legacy_main", "ui_components"):
            self.assertNotIn(f'"{name}"', declared)

    def test_tkinterdnd2_is_not_a_dependency(self):
        # It existed only for the legacy frontend's drag-and-drop.
        for name in ("pyproject.toml", "requirements.txt"):
            self.assertNotIn(
                "tkinterdnd2", _read(name), f"{name} still pins tkinterdnd2"
            )

    def test_no_shipped_module_imports_tkinter(self):
        offenders = []
        for path in REPO_ROOT.glob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if re.search(r"^\s*(import|from)\s+tkinter", text, re.M):
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"Tkinter imports remain in {offenders}")


if __name__ == "__main__":
    unittest.main()
