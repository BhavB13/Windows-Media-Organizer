"""Sign an update manifest with an RSA private key using OpenSSL."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import tempfile
from pathlib import Path


def canonical_bytes(payload: dict) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("private_key", type=Path)
    args = parser.parse_args()

    payload = json.loads(args.manifest.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as temp_dir:
        data_path = Path(temp_dir) / "manifest.canonical.json"
        signature_path = Path(temp_dir) / "manifest.sig"
        data_path.write_bytes(canonical_bytes(payload))
        subprocess.run(
            [
                "openssl",
                "dgst",
                "-sha256",
                "-sign",
                str(args.private_key),
                "-out",
                str(signature_path),
                str(data_path),
            ],
            check=True,
        )
        payload["signature"] = base64.b64encode(signature_path.read_bytes()).decode("ascii")

    args.manifest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
