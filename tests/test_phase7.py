import base64
import hashlib
import json
import os
import tempfile
import unittest
import importlib
from pathlib import Path
from unittest.mock import patch

from duplicate_transfer_manager.core import ServiceError
from duplicate_transfer_manager.core.security import (
    canonical_json,
    sign_rsa_sha256_for_tests,
    verify_rsa_sha256_signature,
)
from duplicate_transfer_manager.runtime_paths import get_runtime_paths
from duplicate_transfer_manager.services import UpdateService
from duplicate_transfer_manager.services.support_services import _compare_versions, _resource_path


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
            original = json.loads((paths.updates / "last_check.json").read_text(encoding="utf-8"))["checked_at"]

            result = service.check_manifest_url("https://github.com/BhavB13/Windows-Media-Organizer/releases/latest/download/update.json")
            self.assertFalse(result["checked"])
            self.assertIn("24 hours", result["reason"])
            self.assertEqual(
                json.loads((paths.updates / "last_check.json").read_text(encoding="utf-8"))["checked_at"],
                original,
            )

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

                def read(self, _size=-1):
                    if getattr(self, "done", False):
                        return b""
                    self.done = True
                    return b"installer-bytes"

                def geturl(self):
                    return manifest["installer_url"]

            with patch(
                "duplicate_transfer_manager.services.support_services.urlopen",
                return_value=Response(),
            ):
                downloaded = service.download_installer(manifest, approved=True)

            self.assertTrue(downloaded.exists())
            self.assertEqual(downloaded.read_bytes(), b"installer-bytes")

    def test_download_rejects_oversize_response_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(temp_dir)
            installer = paths.updates / "source-installer.exe"
            installer.write_bytes(b"ok")
            manifest = _signed_manifest(installer)
            service = UpdateService(paths)

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def read(self, _size=-1):
                    if getattr(self, "done", False):
                        return b""
                    self.done = True
                    return b"too large"

                def geturl(self):
                    return manifest["installer_url"]

            with patch("duplicate_transfer_manager.services.support_services.urlopen", return_value=Response()):
                with self.assertRaises(ServiceError):
                    service.download_installer(manifest, approved=True)

            target = paths.updates / f"DuplicateTransferManagerSetup-{manifest['version']}.exe.partial"
            self.assertFalse(target.exists())

    def test_update_urls_require_https_and_approved_hosts_including_redirects(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = UpdateService(get_runtime_paths(temp_dir))
            for url in ("http://github.com/update.json", "file:///tmp/update.json", "https://example.com/update.json"):
                with self.assertRaises(ServiceError):
                    service.check_manifest_url(url, force=True)

            class Redirected:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def geturl(self):
                    return "https://example.com/redirected.json"

            with patch("duplicate_transfer_manager.services.support_services.urlopen", return_value=Redirected()):
                with self.assertRaises(ServiceError):
                    service.check_manifest_url(
                        "https://github.com/BhavB13/Windows-Media-Organizer/releases/latest/download/update.json",
                        force=True,
                    )

    def test_missing_resource_path_fails_instead_of_searching_working_directory(self):
        with self.assertRaises(FileNotFoundError):
            _resource_path("packaging/definitely-missing-update-key.json")

    def test_manifest_requires_nonempty_publisher_thumbprint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = get_runtime_paths(temp_dir)
            installer = paths.updates / "installer.exe"
            installer.write_bytes(b"installer")
            manifest = _signed_manifest(installer)
            manifest["authenticode_thumbprint"] = ""
            manifest["signature"] = sign_rsa_sha256_for_tests(manifest, PRIVATE_TEST_KEY)

            with self.assertRaises(ServiceError):
                UpdateService(paths).verify_manifest(manifest)

    def test_version_comparison_respects_prerelease_ordering(self):
        self.assertLess(_compare_versions("0.9.0rc1", "0.9"), 0)
        self.assertGreater(_compare_versions("1.0", "1.0rc1"), 0)

    def test_signature_verifier_rejects_non_ff_padding(self):
        payload = {"version": "1.0", "signature": ""}
        modulus = int(PRIVATE_TEST_KEY["n"], 16)
        private_exponent = int(PRIVATE_TEST_KEY["d"], 16)
        key_size = (modulus.bit_length() + 7) // 8
        digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(canonical_json(payload)).digest()
        padding_length = key_size - len(digest_info) - 3
        encoded = b"\x00\x01" + b"\xff" + b"\xfe" + (b"\xff" * (padding_length - 2)) + b"\x00" + digest_info
        signature = pow(int.from_bytes(encoded, "big"), private_exponent, modulus).to_bytes(key_size, "big")
        payload["signature"] = base64.b64encode(signature).decode("ascii")

        self.assertFalse(verify_rsa_sha256_signature(payload, {"n": PRIVATE_TEST_KEY["n"], "e": 65537}))

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

    def test_release_runtime_requirements_include_working_recycle_dependency(self):
        root = Path(__file__).resolve().parents[1]
        requirements = (root / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("Send2Trash==1.8.3", requirements)
        self.assertIsNotNone(importlib.import_module("send2trash"))

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

        # A floor, not an exact match. Google serves current platform-tools only
        # from the -latest- URL, so asserting an exact version broke the release
        # build every time they shipped an update.
        self.assertIn("minimum_version", payload)
        self.assertNotIn("version", payload)
        self.assertIn("platform-tools", payload["download_url"])
        self.assertRegex(payload["minimum_version"], r"^\d+\.\d+\.\d+$")


class PackagingMetadataTests(unittest.TestCase):
    """Guard the metadata a downloaded build shows to a user."""

    root = Path(__file__).resolve().parents[1]

    def test_spec_stamps_a_windows_version_resource_from_version_py(self):
        # Without this the built exe carries no ProductName, CompanyName, or
        # FileVersion at all: the Properties dialog is blank, and an unsigned
        # binary with no version information fares much worse with SmartScreen.
        spec = (self.root / "packaging" / "duplicate_transfer_manager.spec").read_text(encoding="utf-8")
        self.assertIn("version=version_resource", spec)
        self.assertIn("VSVersionInfo", spec)
        for field in ("CompanyName", "FileDescription", "FileVersion", "ProductName", "LegalCopyright"):
            self.assertIn(field, spec)
        # Read from version.py rather than hardcoded, so it cannot drift.
        self.assertIn("version.py", spec)

    def test_manifest_and_version_files_survive_a_byte_order_mark(self):
        # The build writes both of these with PowerShell's Set-Content -Encoding
        # UTF8, which emits a BOM. json.loads rejects a BOM outright, so signing
        # failed on every release build, and the recorded platform-tools version
        # came back with a stray ﻿ glued to the front.
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = Path(temp_dir) / "update-manifest.json"
            manifest.write_bytes(b"\xef\xbb\xbf" + json.dumps({"version": "0.9.0"}).encode("utf-8"))

            with self.assertRaises(json.JSONDecodeError):
                json.loads(manifest.read_text(encoding="utf-8"))

            from duplicate_transfer_manager.services.support_services import UpdateService

            paths = get_runtime_paths(Path(temp_dir) / "data")
            self.assertEqual(UpdateService(paths).load_manifest(manifest)["version"], "0.9.0")

            signer = (self.root / "scripts" / "sign_update_manifest.py").read_text(encoding="utf-8")
            self.assertIn('encoding="utf-8-sig"', signer)

            recorded = Path(temp_dir) / "bundled_version.txt"
            recorded.write_bytes(b"\xef\xbb\xbf37.0.1\n")
            self.assertEqual(recorded.read_text(encoding="utf-8-sig").strip(), "37.0.1")

    def test_installer_writes_into_the_repository_dist_folder(self):
        # OutputDir is relative to the .iss file, so without the prefix the
        # installer lands in packaging\dist and the release script fails its
        # own existence check.
        installer = (self.root / "packaging" / "installer.iss").read_text(encoding="utf-8")
        self.assertIn(r"OutputDir=..\dist\installer", installer)

    def test_installer_can_build_without_a_signing_certificate(self):
        # Signing must be opt-in, or no downloadable build can be produced at
        # all until a certificate exists.
        installer = (self.root / "packaging" / "installer.iss").read_text(encoding="utf-8")
        self.assertIn("#ifdef SIGNED", installer)
        signed_block = installer.split("#ifdef SIGNED", 1)[1].split("#endif", 1)[0]
        self.assertIn("SignTool=signtool", signed_block)
        self.assertIn("SignedUninstaller=yes", signed_block)

        script = (self.root / "scripts" / "build_release.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$SkipSigning", script)
        self.assertIn("/DSIGNED", script)

    def test_installer_shows_the_license_and_publisher_details(self):
        installer = (self.root / "packaging" / "installer.iss").read_text(encoding="utf-8")
        for directive in ("LicenseFile=", "AppPublisherURL=", "AppSupportURL=", "AppCopyright=", "VersionInfoVersion="):
            self.assertIn(directive, installer)


if __name__ == "__main__":
    unittest.main()
