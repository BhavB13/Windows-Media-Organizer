"""Small dependency-free SVG icon set used throughout the application."""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QSize
from PySide6.QtGui import QIcon, QPixmap


PATHS = {
    "overview": '<path d="M3 11 12 4l9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1Z"/><path d="M9 21v-6h6v6"/>',
    "duplicates": '<rect x="7" y="7" width="13" height="13" rx="2"/><path d="M4 17H3a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1h13a1 1 0 0 1 1 1v1"/>',
    "import": '<path d="M12 3v12m0 0 4-4m-4 4-4-4"/><path d="M4 17v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/>',
    "activity": '<path d="M4 6h16M4 12h16M4 18h10"/><circle cx="2" cy="6" r=".5"/><circle cx="2" cy="12" r=".5"/><circle cx="2" cy="18" r=".5"/>',
    "quarantine": '<path d="M12 2 4 5v6c0 5 3.4 9.3 8 11 4.6-1.7 8-6 8-11V5Z"/><path d="m9 9 6 6m0-6-6 6"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1A1.7 1.7 0 0 0 9 4.6 1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/>',
    "help": '<circle cx="12" cy="12" r="10"/><path d="M9.5 9a2.7 2.7 0 1 1 4.5 2c-1.2.9-2 1.4-2 3"/><path d="M12 18h.01"/>',
    "device": '<rect x="6" y="2" width="12" height="20" rx="2"/><path d="M10 18h4"/>',
    "folder": '<path d="M3 5h6l2 2h10v12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Z"/>',
    "drive": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 14h18"/><circle cx="17" cy="16.5" r=".5"/>',
    "phone": '<rect x="7" y="2" width="10" height="20" rx="2"/><path d="M11 18h2"/>',
    "search": '<circle cx="10.5" cy="10.5" r="7.5"/><path d="m16 16 5 5"/>',
    "check": '<path d="m5 12 4 4L19 6"/>',
    "arrow": '<path d="M5 12h14m-5-5 5 5-5 5"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 11v6m0-10h.01"/>',
}


@lru_cache(maxsize=128)
def icon(name: str, color: str = "#667085", size: int = 20) -> QIcon:
    paths = PATHS.get(name, PATHS["info"])
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{paths}</svg>"""
    pixmap = QPixmap()
    pixmap.loadFromData(QByteArray(svg.encode("utf-8")), "SVG")
    return QIcon(pixmap)


def icon_size(size: int = 20) -> QSize:
    return QSize(size, size)
