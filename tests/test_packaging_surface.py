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


class UpdateClaimsMatchBehaviourTests(unittest.TestCase):
    """The privacy notice must not describe requests the app never makes.

    UpdateService can fetch and verify a manifest, but nothing reaches it and no
    release endpoint is configured, so no update request is ever issued.
    """

    def test_privacy_does_not_claim_the_app_fetches_updates(self):
        privacy = _read("PRIVACY.md").casefold()
        self.assertNotIn("fetches a signed update manifest", privacy)

    def test_privacy_states_that_automatic_update_checks_do_not_happen(self):
        privacy = _read("PRIVACY.md").casefold()
        self.assertIn("no release source is configured", privacy)


class UpdateSettingsSurviveTheWidgetRemovalTests(unittest.TestCase):
    """The fields outlive the controls that used to write them.

    AppSettings.from_dict drops unknown keys, so deleting these fields would
    silently discard them from every existing settings.json the first time the
    app loaded and saved.
    """

    def test_the_fields_still_round_trip(self):
        from duplicate_transfer_manager.core import AppSettings

        restored = AppSettings.from_dict(
            AppSettings(
                check_updates_automatically=False, update_channel="beta"
            ).to_dict()
        )
        self.assertFalse(restored.check_updates_automatically)
        self.assertEqual(restored.update_channel, "beta")


class UpdateStatusDoesNotOverstateItselfTests(unittest.TestCase):
    def test_a_present_signing_key_is_not_reported_as_a_configured_release_source(self):
        # "configured" used to mean the bundled public key file exists, which is
        # true in every packaged build and says nothing about whether an update
        # could be found.
        import tempfile
        from duplicate_transfer_manager.runtime_paths import get_runtime_paths
        from duplicate_transfer_manager.services import UpdateService

        with tempfile.TemporaryDirectory() as temp_dir:
            status = UpdateService(get_runtime_paths(temp_dir)).status()

        self.assertFalse(status["release_source_configured"])
        self.assertFalse(status["configured"])
        self.assertFalse(status["check_due"])
        self.assertIn("no release source is configured", status["message"].casefold())


if __name__ == "__main__":
    unittest.main()


class WheelManifestTests(unittest.TestCase):
    """pyproject lists packages and modules explicitly, so anything new must be
    added there by hand. Forgetting one only shows up in an installed wheel,
    which then fails to start even though the checkout runs fine."""

    def _declared(self, key: str) -> set[str]:
        block = re.search(rf"{key}\s*=\s*\[(.*?)\]", _read("pyproject.toml"), re.S)
        self.assertIsNotNone(block, f"{key} block not found in pyproject.toml")
        return set(re.findall(r'"([^"]+)"', block.group(1)))

    def test_every_package_under_src_is_declared(self):
        source = REPO_ROOT / "src"
        on_disk = {
            ".".join(init.parent.relative_to(source).parts)
            for init in source.rglob("__init__.py")
            if "__pycache__" not in init.parts
        }
        missing = sorted(on_disk - self._declared("packages"))
        self.assertEqual(missing, [], f"packages missing from pyproject.toml: {missing}")

    def test_every_top_level_module_is_declared(self):
        on_disk = {path.stem for path in REPO_ROOT.glob("*.py")}
        missing = sorted(on_disk - self._declared("py-modules"))
        self.assertEqual(missing, [], f"py-modules missing from pyproject.toml: {missing}")
