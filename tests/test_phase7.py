import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from duplicate_transfer_manager.core import ServiceError
from duplicate_transfer_manager.core.security import sign_rsa_sha256_for_tests
from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.services import UpdateService


PRIVATE_TEST_KEY = {
    "n": "ccd8ea06a0c0538e735ba61e2c76d34c5653e369e2273a46922caeb20c07ca6b0844a24a2770cc8d2b04c51fe05d79b136f6a1b819d53ac56f3b1e85ca9b8b026342fe7e0736988ef20cf65970428d01085150bf9d7ecf6f5e821afa009db6be2ca9975c379e1d5b11e9c6e6726c352561b3d2f40bc9c422c352aac07ffea61b",
    "d": "79eebc0ec9ecf14f9fb6f4008df304ff317ba9a843279a769b57e17cb5d0855a8487661ac1b350eecea67e37e5337ed64fa32acc0d047181481e66a2b8e131325fd255002cd246fe279e9e2bf290ac7126dfb7e2fbad89ceb7322ba993864bb572973f1109adf54abdd0d74a2dc56622de39ea628e17385085c0b6006a528831",
}


def _signed_manifest(installer: Path, *, version: str = "0.9.0") -> dict:
    payload = {
        "version": version,
        "channel": "stable",
        "installer_url": "https://github.com/BhavB13/Windows-Media-Organizer/releases/download/v0.9.0/DuplicateTransferManagerSetup-0.9.0.exe",
        "size": installer.stat().st_size,
        "sha256": hashlib.sha256(installer.read_bytes()).hexdigest(),
        "release_notes_url": "https://github.com/BhavB13/Windows-Media-Organizer/releases/tag/v0.9.0",
        "minimum_supported_version": "0.6.0",
        "signature_algorithm": "RSASSA-PKCS1-v1_5-SHA256",
        "authenticode_thumbprint": "ABCDEF",
        "publisher": "BhavB13",
    }
    payload["signature"] = sign_rsa_sha256_for_tests(payload, PRIVATE_TEST_KEY)
    return payload


class Phase7PackagingUpdateTests(unittest.TestCase):
    def test_signed_manifest_verifies_installer_size_and_checksum(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(temp_dir)
            installer = paths.updates / "installer.exe"
            installer.write_bytes(b"installer-bytes")
            manifest = _signed_manifest(installer)

            result = UpdateService(paths).verify_manifest(
                manifest,
                installer_path=installer,
                require_newer=True,
            )

        self.assertTrue(result["valid"])
        self.assertTrue(result["signature_verified"])
        self.assertTrue(result["checksum_verified"])
        self.assertFalse(result["authenticode_verified"])

    def test_manifest_refuses_downgrade_or_same_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(temp_dir)
            installer = paths.updates / "installer.exe"
            installer.write_bytes(b"installer-bytes")
            manifest = _signed_manifest(installer, version="0.8.0")

            with self.assertRaises(ServiceError):
                UpdateService(paths).verify_manifest(manifest, installer_path=installer)

    def test_manifest_refuses_bad_signature_and_bad_checksum(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(temp_dir)
            installer = paths.updates / "installer.exe"
            installer.write_bytes(b"installer-bytes")
            manifest = _signed_manifest(installer)
            manifest["sha256"] = "0" * 64

            with self.assertRaises(ServiceError):
                UpdateService(paths).verify_manifest(manifest, installer_path=installer)

            manifest = _signed_manifest(installer)
            manifest["signature"] = manifest["signature"][:-4] + "AAAA"
            with self.assertRaises(ServiceError):
                UpdateService(paths).verify_manifest(manifest, installer_path=installer)

    def test_update_check_cadence_records_and_skips_recent_checks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(temp_dir)
            service = UpdateService(paths)
            service.record_check({"checked": True})

            result = service.check_manifest_url("https://example.invalid/manifest.json")

        self.assertFalse(result["checked"])
        self.assertIn("24 hours", result["reason"])

    def test_download_requires_approval_and_verifies_downloaded_installer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(temp_dir)
            installer = paths.updates / "source-installer.exe"
            installer.write_bytes(b"installer-bytes")
            manifest = _signed_manifest(installer)
            service = UpdateService(paths)

            with self.assertRaises(ServiceError):
                service.download_installer(manifest, approved=False)

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self):
                    return b"installer-bytes"

            with patch(
                "duplicate_transfer_manager.services.support_services.urlopen",
                return_value=Response(),
            ):
                downloaded = service.download_installer(manifest, approved=True)

            self.assertTrue(downloaded.exists())
            self.assertEqual(downloaded.read_bytes(), b"installer-bytes")

    def test_launch_verified_installer_preserves_state_and_requires_approval(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(temp_dir)
            installer = paths.updates / "installer.exe"
            installer.write_bytes(b"installer-bytes")
            manifest = _signed_manifest(installer)
            service = UpdateService(paths)

            with self.assertRaises(ServiceError):
                service.launch_verified_installer(manifest, installer, approved=False)
            if os.name == "nt":
                # The fixture is intentionally unsigned; emulate the verified release artifact
                # and prevent an actual installer process during a Windows-hosted test run.
                with patch.object(service, "verify_authenticode", return_value=True), patch(
                    "duplicate_transfer_manager.services.support_services.subprocess.Popen"
                ):
                    result = service.launch_verified_installer(manifest, installer, approved=True)
            else:
                result = service.launch_verified_installer(manifest, installer, approved=True)

            self.assertEqual(result["launched"], os.name == "nt")
            self.assertTrue(Path(result["state_file"]).exists())
            self.assertTrue(result["verification"]["checksum_verified"])

    def test_release_packaging_files_are_present(self):
        root = Path(__file__).resolve().parents[1]
        expected = [
            root / "packaging" / "duplicate_transfer_manager.spec",
            root / "packaging" / "installer.iss",
            root / "packaging" / "release_manifest.example.json",
            root / "packaging" / "update_public_key.json",
            root / ".github" / "workflows" / "release.yml",
            root / "scripts" / "build_release.ps1",
            root / "scripts" / "create_app_icon.py",
        ]

        for path in expected:
            self.assertTrue(path.exists(), path)

    def test_manifest_example_contains_required_phase7_fields(self):
        root = Path(__file__).resolve().parents[1]
        payload = json.loads((root / "packaging" / "release_manifest.example.json").read_text(encoding="utf-8"))

        for field in (
            "version",
            "channel",
            "installer_url",
            "size",
            "sha256",
            "release_notes_url",
            "minimum_supported_version",
            "signature",
            "authenticode_thumbprint",
        ):
            self.assertIn(field, payload)

    def test_android_platform_tools_manifest_has_download_url(self):
        root = Path(__file__).resolve().parents[1]
        payload = json.loads((root / "packaging" / "android_platform_tools_manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["version"], "37.0.0")
        self.assertIn("platform-tools", payload["download_url"])


if __name__ == "__main__":
    unittest.main()
