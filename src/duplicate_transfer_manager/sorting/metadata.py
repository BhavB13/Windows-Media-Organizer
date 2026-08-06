"""File discovery and metadata extraction for the sorting pipeline."""

from __future__ import annotations

import mimetypes
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Iterable

from ..core import CancellationToken, ErrorCode, OperationReporter, ServiceError
from .models import FileMetadata

try:
    from PIL import Image
except ImportError:  # pragma: no cover - supported fallback for engine-only installs
    Image = None


class MetadataExtractor:
    def extract(self, path: str | Path) -> FileMetadata:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            raise ServiceError(ErrorCode.NOT_FOUND, "A file selected for sorting is unavailable.")
        stat = candidate.stat()
        mime_type, _encoding = mimetypes.guess_type(candidate.name)
        media_type = (mime_type or "application/octet-stream").split("/", 1)[0]
        width = height = 0
        captured = 0.0
        attributes: dict[str, object] = {}
        if Image is not None and media_type == "image":
            try:
                with Image.open(candidate) as image:
                    width, height = image.size
                    attributes["format"] = image.format or ""
                    exif = image.getexif()
                    raw_date = exif.get(36867) or exif.get(306)
                    if raw_date:
                        captured = datetime.strptime(str(raw_date), "%Y:%m:%d %H:%M:%S").timestamp()
            except (OSError, TypeError, ValueError):
                pass
        elif media_type == "video":
            video = self._video_metadata(candidate)
            width = int(video.get("width", 0) or 0)
            height = int(video.get("height", 0) or 0)
            attributes.update(video)
        return FileMetadata(
            path=str(candidate.resolve()), name=candidate.name, extension=candidate.suffix.lower(),
            size=stat.st_size, created=stat.st_ctime, modified=stat.st_mtime,
            media_type=media_type, width=width, height=height, duration=float(attributes.get("duration", 0) or 0), captured=captured,
            mime_type=mime_type or "", attributes=attributes,
        )

    @staticmethod
    def _video_metadata(path: Path) -> dict[str, object]:
        executable = shutil.which("ffprobe")
        if not executable:
            return {}
        try:
            completed = subprocess.run(
                [executable, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,duration:format=duration", "-of", "json", str(path)],
                capture_output=True, text=True, timeout=15, check=False,
            )
            if completed.returncode != 0:
                return {}
            payload = json.loads(completed.stdout or "{}")
            stream = (payload.get("streams") or [{}])[0]
            duration = stream.get("duration") or payload.get("format", {}).get("duration") or 0
            return {"width": int(stream.get("width", 0) or 0), "height": int(stream.get("height", 0) or 0), "duration": float(duration)}
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, json.JSONDecodeError):
            return {}


class SortScanner:
    """Discover explicit files/folders without crossing excluded destinations."""

    def __init__(self, extractor: MetadataExtractor | None = None) -> None:
        self.extractor = extractor or MetadataExtractor()

    def scan(
        self,
        sources: Iterable[str | Path],
        *,
        recursive: bool = True,
        excluded_roots: Iterable[str | Path] = (),
        cancellation: CancellationToken | None = None,
        reporter: OperationReporter | None = None,
    ) -> tuple[FileMetadata, ...]:
        token = cancellation or CancellationToken()
        excluded = tuple(Path(value).expanduser().resolve() for value in excluded_roots)
        discovered: dict[Path, FileMetadata] = {}
        for raw_source in sources:
            token.raise_if_cancelled()
            source = Path(raw_source).expanduser().resolve()
            if not source.exists():
                raise ServiceError(ErrorCode.NOT_FOUND, f"Sorting source is unavailable: {source}")
            candidates = [source] if source.is_file() else source.rglob("*") if recursive else source.glob("*")
            for candidate in candidates:
                token.raise_if_cancelled()
                try:
                    resolved = candidate.resolve()
                    if not resolved.is_file() or any(_within(resolved, root) for root in excluded):
                        continue
                    if resolved in discovered:
                        continue
                    discovered[resolved] = self.extractor.extract(resolved)
                    if reporter and len(discovered) % 50 == 0:
                        reporter.progress_callback(len(discovered), 0, f"Scanning files: {len(discovered)} found")
                except (OSError, ServiceError):
                    continue
        return tuple(discovered[path] for path in sorted(discovered, key=lambda value: str(value).casefold()))


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
