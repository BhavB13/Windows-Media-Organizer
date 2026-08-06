"""Privacy and update-verification helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from typing import Any


SENSITIVE_KEYS = {
    "path",
    "paths",
    "file",
    "filename",
    "current_item",
    "device_serial",
    "serial",
    "hash",
    "stored_path",
    "original_path",
    "report_path",
    "runtime_root",
    "bundled_adb",
    "system_adb",
}

PATH_PATTERN = re.compile(
    r"(?P<path>(?:[A-Za-z]:\\|\\\\|/)(?:[^\s,;:'\"]+[\\/])*[^\s,;:'\"]+)"
)
FILENAME_PATTERN = re.compile(
    r"(?<![\w.])(?P<filename>[^\s,;:'\"\\/]+\.[A-Za-z][A-Za-z0-9]{0,9})(?![\w.])"
)
HASH_PATTERN = re.compile(r"\b[a-fA-F0-9]{32,128}\b")
ANDROID_SERIAL_PATTERN = re.compile(
    r"\b[A-Z0-9]{6,}:[0-9]{4,5}\b|\b(?=[A-Z0-9]{8,}\b)(?=[A-Z0-9]*[0-9])[A-Z0-9]+\b"
)
_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex(
    "3031300d060960864801650304020105000420"
)


def correlation_id(value: Any, *, prefix: str = "local") -> str:
    digest = hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()
    return f"{prefix}-{digest[:12]}"


def sanitize_text(value: str) -> str:
    """Redact sensitive local identifiers while preserving useful shape."""

    def path_replacement(match: re.Match[str]) -> str:
        return f"<redacted:path:{correlation_id(match.group('path'), prefix='p')}>"

    sanitized = PATH_PATTERN.sub(path_replacement, value)
    sanitized = FILENAME_PATTERN.sub(
        lambda match: f"<redacted:filename:{correlation_id(match.group('filename'), prefix='f')}>",
        sanitized,
    )
    sanitized = HASH_PATTERN.sub(
        lambda match: f"<redacted:hash:{correlation_id(match.group(0), prefix='h')}>",
        sanitized,
    )
    return ANDROID_SERIAL_PATTERN.sub(
        lambda match: f"<redacted:serial:{correlation_id(match.group(0), prefix='d')}>",
        sanitized,
    )


def sanitize_payload(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()
    if isinstance(value, str):
        sensitive_key = (
            lowered in SENSITIVE_KEYS
            or lowered.endswith("_path")
            or lowered.endswith("_paths")
            or lowered.endswith("_serial")
            or lowered.endswith("_hash")
        )
        if sensitive_key:
            return f"<redacted:{lowered or 'value'}:{correlation_id(value)}>"
        return sanitize_text(value)
    if isinstance(value, Mapping):
        return {
            str(child_key): sanitize_payload(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [sanitize_payload(item, key=key) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_payload(item, key=key) for item in value)
    return value


def canonical_json(payload: Mapping[str, Any]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    return json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def verify_rsa_sha256_signature(payload: Mapping[str, Any], public_key: Mapping[str, Any]) -> bool:
    signature = str(payload.get("signature", ""))
    if not signature:
        return False
    try:
        signature_bytes = base64.b64decode(signature, validate=True)
        modulus = int(str(public_key["n"]), 16)
        exponent = int(public_key.get("e", 65537))
    except (KeyError, TypeError, ValueError):
        return False

    key_size = (modulus.bit_length() + 7) // 8
    if len(signature_bytes) != key_size:
        return False

    decoded = pow(int.from_bytes(signature_bytes, "big"), exponent, modulus).to_bytes(
        key_size,
        "big",
    )
    digest = hashlib.sha256(canonical_json(payload)).digest()
    expected_tail = _SHA256_DIGEST_INFO_PREFIX + digest
    if not decoded.startswith(b"\x00\x01"):
        return False
    separator_index = decoded.find(b"\x00", 2)
    if separator_index < 10:
        return False
    if decoded[2:separator_index] != b"\xff" * (separator_index - 2):
        return False
    return hmac.compare_digest(decoded[separator_index + 1 :], expected_tail)


def sign_rsa_sha256_for_tests(payload: Mapping[str, Any], private_key: Mapping[str, Any]) -> str:
    """Create a PKCS#1 v1.5 signature for tests without external dependencies."""

    modulus = int(str(private_key["n"]), 16)
    private_exponent = int(str(private_key["d"]), 16)
    key_size = (modulus.bit_length() + 7) // 8
    digest_info = _SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(canonical_json(payload)).digest()
    padding_length = key_size - len(digest_info) - 3
    if padding_length < 8:
        raise ValueError("RSA key is too small for SHA-256 signature padding.")
    encoded = b"\x00\x01" + (b"\xff" * padding_length) + b"\x00" + digest_info
    signature = pow(int.from_bytes(encoded, "big"), private_exponent, modulus).to_bytes(
        key_size,
        "big",
    )
    return base64.b64encode(signature).decode("ascii")
