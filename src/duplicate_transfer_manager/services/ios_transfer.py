"""Placeholder interfaces for future iOS import support.

These contracts intentionally do not implement iOS access yet. They give the
application a stable place to integrate an iOS provider later without mixing
future platform work into the existing local/ADB transfer paths.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IOSDevice:
    identifier: str
    name: str
    status: str = "unsupported"


class IOSDeviceProvider:
    """Future provider boundary for iPhone/iPad discovery."""

    def list_devices(self) -> list[IOSDevice]:
        return []


class IOSImportAdapter:
    """Future adapter boundary for iOS media import."""

    supported = False

    def describe(self) -> str:
        return "iOS transfer support coming soon."


class IOSTransferService:
    """Placeholder service used by the UI to advertise future support safely."""

    def __init__(self, provider: IOSDeviceProvider | None = None) -> None:
        self.provider = provider or IOSDeviceProvider()
        self.adapter = IOSImportAdapter()

    def status(self) -> dict[str, object]:
        return {
            "supported": False,
            "message": "iOS transfer support coming soon.",
            "devices": self.provider.list_devices(),
        }
