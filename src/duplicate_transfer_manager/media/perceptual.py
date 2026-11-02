"""Framework-neutral perceptual image similarity and keep-recommendation engine.

Implements 64-bit perceptual image hashes (average hash, difference hash, and
DCT-II perceptual hash) in pure Python on top of Pillow, orientation-invariant
hash sets honouring EXIF orientation tags, sub-quadratic prefix-bucketed
union-find similarity grouping, and deterministic non-destructive keep
recommendations.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Protocol, Sequence

try:
    from PIL import Image
except ImportError:  # pragma: no cover - supported fallback for engine-only installs
    Image = None

HASH_BITS: int = 64
DEFAULT_MAX_DISTANCE: int = 8
SUPPORTED_ALGORITHMS: tuple[str, ...] = ("ahash", "dhash", "phash")

_ALGORITHM_ALIASES: dict[str, str] = {
    "ahash": "ahash",
    "average": "ahash",
    "average_hash": "ahash",
    "avg": "ahash",
    "avg_hash": "ahash",
    "dhash": "dhash",
    "diff": "dhash",
    "diff_hash": "dhash",
    "difference": "dhash",
    "difference_hash": "dhash",
    "phash": "phash",
    "perceptual": "phash",
    "perceptual_hash": "phash",
    "dct": "phash",
    "dct_hash": "phash",
}

# EXIF tags indicating genuine camera/capture metadata
_EXIF_ORIENTATION_TAG: int = 0x0112  # 274: Orientation
_EXIF_IFD_POINTER_TAG: int = 0x8769  # 34665: ExifOffset sub-IFD
_EXIF_CAPTURE_TAGS: tuple[int, ...] = (
    0x9003,  # 36867: DateTimeOriginal
    0x9004,  # 36868: DateTimeDigitized
    0x0132,  # 306: DateTime
    0x010F,  # 271: Make
    0x0110,  # 272: Model
)


class StopEventLike(Protocol):
    """Protocol matching ``threading.Event`` and ``CancellationToken``."""

    def is_set(self) -> bool:
        ...


def _is_cancelled(stop_event: StopEventLike | None) -> bool:
    return bool(stop_event is not None and stop_event.is_set())


def normalize_algorithm(algorithm: str) -> str:
    """Normalize an algorithm identifier to its canonical short name."""
    if not isinstance(algorithm, str):
        raise TypeError(f"Algorithm name must be a string, got {type(algorithm).__name__}.")
    cleaned = algorithm.strip().lower()
    if not cleaned:
        raise ValueError("Algorithm name must not be empty.")
    return _ALGORITHM_ALIASES.get(cleaned, cleaned)


class _HexDigest(str):
    """String subclass returned by ``ImageHash.to_hex()``.

    Behaves identically to a standard ``str`` while retaining the originating
    algorithm and bit width so ``ImageHash.from_hex(h.to_hex())`` round-trips
    even when the caller omits the ``algorithm`` parameter.
    """

    _algorithm: str
    _bit_count: int

    def __new__(cls, value: str, *, algorithm: str = "phash", bit_count: int = HASH_BITS) -> _HexDigest:
        instance = super().__new__(cls, value)
        instance._algorithm = algorithm
        instance._bit_count = bit_count
        return instance


@dataclass(frozen=True)
class ImageHash:
    """Immutable fixed-width perceptual hash value."""

    algorithm: str
    bits: int
    bit_count: int = HASH_BITS

    def __post_init__(self) -> None:
        normalized_algo = normalize_algorithm(self.algorithm)
        object.__setattr__(self, "algorithm", normalized_algo)

        if not isinstance(self.bit_count, int) or isinstance(self.bit_count, bool) or self.bit_count <= 0:
            raise ValueError(f"bit_count must be a positive integer, got {self.bit_count!r}.")

        raw_bits: Any = self.bits
        if isinstance(raw_bits, (tuple, list)):
            if len(raw_bits) != self.bit_count:
                raise ValueError(f"Bit sequence length {len(raw_bits)} does not match bit_count {self.bit_count}.")
            acc = 0
            for b in raw_bits:
                acc = (acc << 1) | (1 if b else 0)
            raw_bits = acc

        if not isinstance(raw_bits, int) or isinstance(raw_bits, bool):
            raise TypeError(f"bits must be an int, got {type(raw_bits).__name__}.")
        max_val = (1 << self.bit_count) - 1
        if raw_bits < 0 or raw_bits > max_val:
            raise ValueError(f"bits value {raw_bits} out of range for {self.bit_count}-bit hash.")
        object.__setattr__(self, "bits", int(raw_bits))

    def hamming_distance(self, other: ImageHash) -> int:
        """Return the number of differing bits between two hashes of the same algorithm."""
        if not isinstance(other, ImageHash):
            raise TypeError(f"Expected ImageHash, got {type(other).__name__}.")
        if self.algorithm != other.algorithm:
            raise ValueError(
                f"Cannot compare hashes from different algorithms: {self.algorithm!r} vs {other.algorithm!r}."
            )
        if self.bit_count != other.bit_count:
            raise ValueError(
                f"Cannot compare hashes with different bit counts: {self.bit_count} vs {other.bit_count}."
            )
        return (self.bits ^ other.bits).bit_count()

    def to_hex(self) -> str:
        """Serialize the hash bits as a zero-padded lowercase hexadecimal string."""
        width = (self.bit_count + 3) // 4
        return _HexDigest(f"{self.bits:0{width}x}", algorithm=self.algorithm, bit_count=self.bit_count)

    @classmethod
    def from_hex(
        cls,
        hex_value: str,
        algorithm: str | None = None,
        bit_count: int | None = None,
    ) -> ImageHash:
        """Reconstruct an ``ImageHash`` from its hexadecimal representation."""
        if not isinstance(hex_value, str):
            raise TypeError(f"hex_value must be a string, got {type(hex_value).__name__}.")

        attached_algo: str | None = getattr(hex_value, "_algorithm", None)
        attached_bits: int | None = getattr(hex_value, "_bit_count", None)

        text = hex_value.strip()
        parsed_algo: str | None = None
        if ":" in text:
            prefix, remainder = text.split(":", 1)
            parsed_algo = prefix.strip()
            text = remainder.strip()

        if text.startswith(("0x", "0X")):
            text = text[2:]
        if not text or text.startswith(("+", "-")):
            raise ValueError(f"Invalid hexadecimal hash string: {hex_value!r}.")

        try:
            value = int(text, 16)
        except ValueError as exc:
            raise ValueError(f"Invalid hexadecimal hash string: {hex_value!r}.") from exc

        resolved_bits = bit_count if bit_count is not None else (attached_bits or max(HASH_BITS, len(text) * 4))
        if len(text) * 4 > resolved_bits and value >= (1 << resolved_bits):
            raise ValueError(
                f"Hex string {hex_value!r} exceeds {resolved_bits}-bit capacity."
            )
        resolved_algo = algorithm or parsed_algo or attached_algo or "phash"
        return cls(algorithm=resolved_algo, bits=value, bit_count=resolved_bits)

    def __int__(self) -> int:
        return self.bits

    def __str__(self) -> str:
        return str(self.to_hex())


@dataclass(frozen=True)
class ImageAnalysisError:
    """Structured error recorded when an image cannot be read or decoded."""

    path: str
    message: str

    @property
    def error(self) -> str:
        return self.message

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass(frozen=True)
class HashedImageEntry:
    """An image path paired with its primary hash and optional rotation variants."""

    path: str
    image_hash: ImageHash
    rotation_hashes: tuple[ImageHash, ...] = ()

    @property
    def all_hashes(self) -> tuple[ImageHash, ...]:
        if self.rotation_hashes:
            return self.rotation_hashes
        return (self.image_hash,)


@dataclass(frozen=True)
class BatchHashResult:
    """Result of hashing a batch of image paths."""

    entries: tuple[HashedImageEntry, ...] = ()
    errors: tuple[ImageAnalysisError, ...] = ()
    cancelled: bool = False


@dataclass(frozen=True)
class SimilarGroup(Sequence[str]):
    """A transitive cluster of perceptually similar images in deterministic path order."""

    paths: tuple[str, ...]
    entries: tuple[HashedImageEntry, ...] = ()
    algorithm: str = "phash"

    def __len__(self) -> int:
        return len(self.paths)

    def __iter__(self) -> Iterator[str]:
        return iter(self.paths)

    def __getitem__(self, index: int | slice) -> str | tuple[str, ...]:  # type: ignore[override]
        return self.paths[index]

    def __contains__(self, item: object) -> bool:
        if isinstance(item, Path):
            item = str(item)
        return item in self.paths

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SimilarGroup):
            return self.paths == other.paths and self.algorithm == other.algorithm
        if isinstance(other, (tuple, list)):
            return tuple(str(x) for x in other) == self.paths
        return NotImplemented


@dataclass(frozen=True)
class SimilarityGroupingResult(Sequence[SimilarGroup]):
    """Result returned by ``find_similar_groups``.

    Behaves as a sequence of ``SimilarGroup`` objects while also exposing
    ``.groups``, ``.errors``, and ``.cancelled`` attributes for batch reporting.
    """

    groups: tuple[SimilarGroup, ...] = ()
    errors: tuple[ImageAnalysisError, ...] = ()
    cancelled: bool = False

    def __len__(self) -> int:
        return len(self.groups)

    def __iter__(self) -> Iterator[SimilarGroup]:
        return iter(self.groups)

    def __getitem__(self, index: int | slice) -> SimilarGroup | tuple[SimilarGroup, ...]:  # type: ignore[override]
        return self.groups[index]

    def __bool__(self) -> bool:
        return bool(self.groups)


@dataclass(frozen=True)
class CandidateScore:
    """Quality metrics used to rank duplicate candidates for keep recommendation."""

    path: str
    width: int
    height: int
    pixel_count: int
    byte_size: int
    has_exif_capture: bool
    mtime: float

    def sort_key(self) -> tuple[int, int, int, float, str]:
        """Deterministic sort key (smallest key wins)."""
        return (
            -self.pixel_count,
            -self.byte_size,
            0 if self.has_exif_capture else 1,
            self.mtime,
            self.path,
        )


@dataclass(frozen=True)
class RejectedCandidate:
    """A non-keeper candidate and the human-readable explanation for rejecting it."""

    path: str
    reason: str
    score: CandidateScore | None = None


@dataclass(frozen=True)
class KeepRecommendation:
    """Non-destructive recommendation identifying which candidate to keep and why."""

    keeper: str
    keeper_score: CandidateScore | None = None
    reasons: dict[str, str] = field(default_factory=dict)
    rejected: tuple[RejectedCandidate, ...] = ()
    rankings: tuple[CandidateScore, ...] = ()
    errors: tuple[ImageAnalysisError, ...] = ()
    cancelled: bool = False

    @property
    def keeper_path(self) -> str:
        return self.keeper

    @property
    def rejected_reasons(self) -> dict[str, str]:
        return self.reasons

    def reason_for(self, path: str | Path) -> str:
        """Return the rejection reason for ``path``."""
        key = str(path)
        if key in self.reasons:
            return self.reasons[key]
        norm = os.path.normcase(os.path.abspath(key))
        for cand_path, reason in self.reasons.items():
            if os.path.normcase(os.path.abspath(cand_path)) == norm:
                return reason
        raise KeyError(f"No rejection reason recorded for {key!r}.")


def _require_pillow() -> Any:
    if Image is None:
        raise RuntimeError("Pillow (PIL) is required for perceptual image analysis.")
    return Image


def _resample_filter() -> int:
    pil_image = _require_pillow()
    resampling = getattr(pil_image, "Resampling", pil_image)
    return int(getattr(resampling, "LANCZOS", getattr(resampling, "BICUBIC", 1)))


def _apply_exif_orientation(image: Any) -> Any:
    """Transpose ``image`` according to its EXIF Orientation tag (0x0112), if present."""
    pil_image = _require_pillow()
    try:
        exif = image.getexif()
        orientation = exif.get(_EXIF_ORIENTATION_TAG) if exif else None
    except Exception:
        orientation = None

    if not orientation or orientation == 1:
        return image

    transpose_ns = getattr(pil_image, "Transpose", pil_image)
    mapping = {
        2: getattr(transpose_ns, "FLIP_LEFT_RIGHT", 0),
        3: getattr(transpose_ns, "ROTATE_180", 3),
        4: getattr(transpose_ns, "FLIP_TOP_BOTTOM", 1),
        5: getattr(transpose_ns, "TRANSPOSE", 5),
        6: getattr(transpose_ns, "ROTATE_270", 4),
        7: getattr(transpose_ns, "TRANSVERSE", 6),
        8: getattr(transpose_ns, "ROTATE_90", 2),
    }
    try:
        code = int(orientation)
    except (TypeError, ValueError):
        return image
    op = mapping.get(code)
    if op is None:
        return image
    return image.transpose(op)


def _load_oriented_grayscale(path_or_image: str | Path | Any) -> Any:
    """Load an image, honour its EXIF orientation tag, and convert to 8-bit grayscale."""
    pil_image = _require_pillow()
    if hasattr(path_or_image, "convert") and hasattr(path_or_image, "resize"):
        oriented = _apply_exif_orientation(path_or_image)
        return oriented.convert("L")

    candidate = Path(os.fspath(path_or_image))
    with pil_image.open(candidate) as img:
        img.load()
        oriented = _apply_exif_orientation(img)
        return oriented.convert("L")


def _compute_ahash_from_gray(gray_image: Any) -> ImageHash:
    resized = gray_image.resize((8, 8), _resample_filter())
    pixels = list(resized.getdata())
    mean = sum(pixels) / 64.0
    bits = 0
    for px in pixels:
        bits = (bits << 1) | (1 if px >= mean else 0)
    return ImageHash(algorithm="ahash", bits=bits, bit_count=HASH_BITS)


def _compute_dhash_from_gray(gray_image: Any) -> ImageHash:
    resized = gray_image.resize((9, 8), _resample_filter())
    pixels = list(resized.getdata())
    bits = 0
    for row in range(8):
        offset = row * 9
        for col in range(8):
            bits = (bits << 1) | (1 if pixels[offset + col + 1] > pixels[offset + col] else 0)
    return ImageHash(algorithm="dhash", bits=bits, bit_count=HASH_BITS)


# Precomputed cosine table for 32-point 1D Type-II DCT:
# C[k][n] = scale(k) * cos(pi * (2n + 1) * k / (2 * N))
_DCT_SIZE: int = 32
_DCT_LOW_FREQ: int = 8


def _build_dct_table(size: int) -> tuple[tuple[float, ...], ...]:
    scale0 = math.sqrt(1.0 / size)
    scale_k = math.sqrt(2.0 / size)
    rows: list[tuple[float, ...]] = []
    for k in range(size):
        factor = scale0 if k == 0 else scale_k
        row = tuple(
            factor * math.cos(math.pi * (2 * n + 1) * k / (2.0 * size))
            for n in range(size)
        )
        rows.append(row)
    return tuple(rows)


_DCT_TABLE_32: tuple[tuple[float, ...], ...] = _build_dct_table(_DCT_SIZE)


def _dct_1d(vector: Sequence[float]) -> list[float]:
    """Pure-Python 1D orthonormal Type-II Discrete Cosine Transform (DCT-II)."""
    n_len = len(vector)
    if n_len == 0:
        return []
    table = _DCT_TABLE_32 if n_len == _DCT_SIZE else _build_dct_table(n_len)
    return [
        sum(vector[n] * table[k][n] for n in range(n_len))
        for k in range(n_len)
    ]


def _dct_2d(
    matrix: Sequence[Sequence[float]],
    *,
    keep_Coeffs: int | None = None,
) -> list[list[float]]:
    """Pure-Python 2D separable Type-II Discrete Cosine Transform (DCT-II).

    When ``keep_Coeffs`` is given (e.g. 8 for a 32x32 pHash grid), only the
    top-left ``keep_Coeffs x keep_Coeffs`` low-frequency block is evaluated.
    """
    rows = len(matrix)
    if rows == 0:
        return []
    cols = len(matrix[0])
    row_table = _DCT_TABLE_32 if cols == _DCT_SIZE else _build_dct_table(cols)
    col_table = _DCT_TABLE_32 if rows == _DCT_SIZE else _build_dct_table(rows)

    out_rows = min(rows, keep_Coeffs) if keep_Coeffs is not None else rows
    out_cols = min(cols, keep_Coeffs) if keep_Coeffs is not None else cols

    # Transform rows for the first `out_cols` horizontal frequencies
    intermediate: list[list[float]] = []
    for r in range(rows):
        src_row = matrix[r]
        transformed_row = [
            sum(src_row[c] * row_table[v][c] for c in range(cols))
            for v in range(out_cols)
        ]
        intermediate.append(transformed_row)

    # Transform columns for the first `out_rows` vertical frequencies
    result: list[list[float]] = []
    for u in range(out_rows):
        cos_u = col_table[u]
        coeff_row = [
            sum(intermediate[r][v] * cos_u[r] for r in range(rows))
            for v in range(out_cols)
        ]
        result.append(coeff_row)
    return result


def _compute_phash_from_gray(gray_image: Any) -> ImageHash:
    resized = gray_image.resize((_DCT_SIZE, _DCT_SIZE), _resample_filter())
    pixels = list(resized.getdata())
    matrix = [
        [float(pixels[row * _DCT_SIZE + col]) for col in range(_DCT_SIZE)]
        for row in range(_DCT_SIZE)
    ]
    low_freq = _dct_2d(matrix, keep_Coeffs=_DCT_LOW_FREQ)

    # Compute the median over the 63 AC coefficients (excluding the [0][0] DC term)
    ac_coeffs = sorted(
        low_freq[u][v]
        for u in range(_DCT_LOW_FREQ)
        for v in range(_DCT_LOW_FREQ)
        if not (u == 0 and v == 0)
    )
    median = ac_coeffs[len(ac_coeffs) // 2]

    bits = 0
    for u in range(_DCT_LOW_FREQ):
        for v in range(_DCT_LOW_FREQ):
            bits = (bits << 1) | (1 if low_freq[u][v] > median else 0)
    return ImageHash(algorithm="phash", bits=bits, bit_count=HASH_BITS)


def average_hash(
    path_or_image: str | Path | Any,
    *,
    stop_event: StopEventLike | None = None,
) -> ImageHash:
    """Compute the 64-bit average hash (aHash) of an image."""
    if _is_cancelled(stop_event):
        return ImageHash(algorithm="ahash", bits=0, bit_count=HASH_BITS)
    gray = _load_oriented_grayscale(path_or_image)
    if _is_cancelled(stop_event):
        return ImageHash(algorithm="ahash", bits=0, bit_count=HASH_BITS)
    return _compute_ahash_from_gray(gray)


def difference_hash(
    path_or_image: str | Path | Any,
    *,
    stop_event: StopEventLike | None = None,
) -> ImageHash:
    """Compute the 64-bit horizontal gradient difference hash (dHash) of an image."""
    if _is_cancelled(stop_event):
        return ImageHash(algorithm="dhash", bits=0, bit_count=HASH_BITS)
    gray = _load_oriented_grayscale(path_or_image)
    if _is_cancelled(stop_event):
        return ImageHash(algorithm="dhash", bits=0, bit_count=HASH_BITS)
    return _compute_dhash_from_gray(gray)


def perceptual_hash(
    path_or_image: str | Path | Any,
    *,
    stop_event: StopEventLike | None = None,
) -> ImageHash:
    """Compute the 64-bit DCT-II perceptual hash (pHash) of an image."""
    if _is_cancelled(stop_event):
        return ImageHash(algorithm="phash", bits=0, bit_count=HASH_BITS)
    gray = _load_oriented_grayscale(path_or_image)
    if _is_cancelled(stop_event):
        return ImageHash(algorithm="phash", bits=0, bit_count=HASH_BITS)
    return _compute_phash_from_gray(gray)


# Standard short aliases
ahash = average_hash
dhash = difference_hash
phash = perceptual_hash


def _compute_hash_from_gray(gray_image: Any, algorithm: str) -> ImageHash:
    canonical = normalize_algorithm(algorithm)
    if canonical == "ahash":
        return _compute_ahash_from_gray(gray_image)
    if canonical == "dhash":
        return _compute_dhash_from_gray(gray_image)
    if canonical == "phash":
        return _compute_phash_from_gray(gray_image)
    raise ValueError(f"Unsupported perceptual hash algorithm: {algorithm!r}.")


def compute_image_hash(
    path_or_image: str | Path | Any,
    algorithm: str = "phash",
    *,
    stop_event: StopEventLike | None = None,
) -> ImageHash:
    """Compute a 64-bit ``ImageHash`` using the requested ``algorithm``."""
    canonical = normalize_algorithm(algorithm)
    if canonical not in SUPPORTED_ALGORITHMS:
        raise ValueError(f"Unsupported perceptual hash algorithm: {algorithm!r}.")
    if _is_cancelled(stop_event):
        return ImageHash(algorithm=canonical, bits=0, bit_count=HASH_BITS)
    gray = _load_oriented_grayscale(path_or_image)
    if _is_cancelled(stop_event):
        return ImageHash(algorithm=canonical, bits=0, bit_count=HASH_BITS)
    return _compute_hash_from_gray(gray, canonical)


def orientation_invariant_hashes(
    path_or_image: str | Path | Any,
    algorithm: str = "phash",
    *,
    stop_event: StopEventLike | None = None,
) -> tuple[ImageHash, ...]:
    """Return hashes for the EXIF-normalized image and its 90/180/270 degree rotations.

    Returns an empty tuple promptly if ``stop_event`` is set before completion.
    """
    canonical = normalize_algorithm(algorithm)
    if canonical not in SUPPORTED_ALGORITHMS:
        raise ValueError(f"Unsupported perceptual hash algorithm: {algorithm!r}.")
    if _is_cancelled(stop_event):
        return ()

    pil_image = _require_pillow()
    gray = _load_oriented_grayscale(path_or_image)
    transpose_ns = getattr(pil_image, "Transpose", pil_image)
    rotations = (
        None,
        getattr(transpose_ns, "ROTATE_90", 2),
        getattr(transpose_ns, "ROTATE_180", 3),
        getattr(transpose_ns, "ROTATE_270", 4),
    )

    hashes: list[ImageHash] = []
    for rot in rotations:
        if _is_cancelled(stop_event):
            return ()
        rotated = gray if rot is None else gray.transpose(rot)
        hashes.append(_compute_hash_from_gray(rotated, canonical))
    return tuple(hashes)


def min_rotation_distance(
    hashes_a: ImageHash | Sequence[ImageHash],
    hashes_b: ImageHash | Sequence[ImageHash],
) -> int:
    """Return the minimum Hamming distance across two orientation hash sets."""
    seq_a = (hashes_a,) if isinstance(hashes_a, ImageHash) else tuple(hashes_a)
    seq_b = (hashes_b,) if isinstance(hashes_b, ImageHash) else tuple(hashes_b)
    if not seq_a or not seq_b:
        raise ValueError("Cannot compare empty orientation hash sequences.")
    return min(ha.hamming_distance(hb) for ha in seq_a for hb in seq_b)


def hash_images(
    paths: Iterable[str | Path],
    algorithm: str = "phash",
    *,
    rotation_invariant: bool = False,
    stop_event: StopEventLike | None = None,
) -> BatchHashResult:
    """Hash a batch of image paths, recording corrupt/unreadable files as errors."""
    canonical = normalize_algorithm(algorithm)
    if canonical not in SUPPORTED_ALGORITHMS:
        raise ValueError(f"Unsupported perceptual hash algorithm: {algorithm!r}.")

    entries: list[HashedImageEntry] = []
    errors: list[ImageAnalysisError] = []

    for raw_path in paths:
        if _is_cancelled(stop_event):
            return BatchHashResult(entries=tuple(entries), errors=tuple(errors), cancelled=True)
        path_str = str(raw_path)
        try:
            if rotation_invariant:
                rot_hashes = orientation_invariant_hashes(raw_path, canonical, stop_event=stop_event)
                if _is_cancelled(stop_event) or not rot_hashes:
                    return BatchHashResult(entries=tuple(entries), errors=tuple(errors), cancelled=True)
                entries.append(
                    HashedImageEntry(
                        path=path_str,
                        image_hash=rot_hashes[0],
                        rotation_hashes=rot_hashes,
                    )
                )
            else:
                img_hash = compute_image_hash(raw_path, canonical, stop_event=stop_event)
                if _is_cancelled(stop_event):
                    return BatchHashResult(entries=tuple(entries), errors=tuple(errors), cancelled=True)
                entries.append(HashedImageEntry(path=path_str, image_hash=img_hash))
        except Exception as exc:
            errors.append(ImageAnalysisError(path=path_str, message=str(exc) or type(exc).__name__))

    return BatchHashResult(entries=tuple(entries), errors=tuple(errors), cancelled=False)


class _UnionFind:
    """Disjoint-set (Union-Find) data structure with path compression and rank heuristic."""

    def __init__(self, size: int) -> None:
        self.parent: list[int] = list(range(size))
        self.rank: list[int] = [0] * size

    def find(self, item: int) -> int:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        curr = item
        while curr != root:
            nxt = self.parent[curr]
            self.parent[curr] = root
            curr = nxt
        return root

    def union(self, a: int, b: int) -> None:
        root_a = self.find(a)
        root_b = self.find(b)
        if root_a == root_b:
            return
        if self.rank[root_a] < self.rank[root_b]:
            root_a, root_b = root_b, root_a
        self.parent[root_b] = root_a
        if self.rank[root_a] == self.rank[root_b]:
            self.rank[root_a] += 1


def _partition_bit_slices(bit_count: int, max_distance: int) -> tuple[tuple[int, int], ...]:
    """Compute ``(shift, mask)`` slices for multi-index pigeonhole prefix bucketing.

    By the pigeonhole principle, if a ``bit_count``-bit hash is partitioned into
    ``B = max_distance + 1`` disjoint bit slices, any two hashes with Hamming
    distance ``<= max_distance`` are guaranteed to share at least one identical
    slice (zero differing bits within that slice). For ``max_distance == 0``, a
    single full-width 64-bit bucket is used. For very large thresholds where
    ``max_distance + 1 > 32``, a single catch-all bucket is used so exactness is
    still preserved.
    """
    if max_distance <= 0:
        return ((0, (1 << bit_count) - 1),)
    num_bands = max_distance + 1
    if num_bands > bit_count // 2:
        return ((0, 0),)

    slices: list[tuple[int, int]] = []
    base_width = bit_count // num_bands
    remainder = bit_count % num_bands
    shift = 0
    for band in range(num_bands):
        width = base_width + (1 if band < remainder else 0)
        mask = (1 << width) - 1
        slices.append((shift, mask))
        shift += width
    return tuple(slices)


def _coerce_entry(
    raw_entry: Any,
    canonical_algo: str,
    *,
    rotation_invariant: bool,
    stop_event: StopEventLike | None,
) -> HashedImageEntry:
    """Convert a supported caller entry into a ``HashedImageEntry``."""
    if isinstance(raw_entry, HashedImageEntry):
        if raw_entry.image_hash.algorithm != canonical_algo:
            raise ValueError(
                f"Entry hash algorithm {raw_entry.image_hash.algorithm!r} does not match {canonical_algo!r}."
            )
        for rot_hash in raw_entry.rotation_hashes:
            if rot_hash.algorithm != canonical_algo:
                raise ValueError(
                    f"Entry rotation hash algorithm {rot_hash.algorithm!r} does not match {canonical_algo!r}."
                )
        return raw_entry

    if isinstance(raw_entry, tuple) and len(raw_entry) == 2:
        raw_path, raw_hash = raw_entry
        path_str = str(raw_path)
        if isinstance(raw_hash, ImageHash):
            if raw_hash.algorithm != canonical_algo:
                raise ValueError(
                    f"Entry hash algorithm {raw_hash.algorithm!r} does not match {canonical_algo!r}."
                )
            return HashedImageEntry(path=path_str, image_hash=raw_hash)
        if isinstance(raw_hash, Sequence) and raw_hash and all(isinstance(h, ImageHash) for h in raw_hash):
            rot_tuple = tuple(raw_hash)
            for rot_hash in rot_tuple:
                if rot_hash.algorithm != canonical_algo:
                    raise ValueError(
                        f"Entry rotation hash algorithm {rot_hash.algorithm!r} does not match {canonical_algo!r}."
                    )
            return HashedImageEntry(path=path_str, image_hash=rot_tuple[0], rotation_hashes=rot_tuple)
        if isinstance(raw_hash, str):
            parsed = ImageHash.from_hex(raw_hash, algorithm=canonical_algo)
            return HashedImageEntry(path=path_str, image_hash=parsed)

    # Object with a `.path` attribute (e.g. models.FileInfo) or a filesystem path
    raw_path = getattr(raw_entry, "path", raw_entry)
    path_str = str(raw_path)
    if rotation_invariant:
        rot_hashes = orientation_invariant_hashes(raw_path, canonical_algo, stop_event=stop_event)
        if not rot_hashes:
            raise RuntimeError("Operation cancelled.")
        return HashedImageEntry(path=path_str, image_hash=rot_hashes[0], rotation_hashes=rot_hashes)

    computed = compute_image_hash(raw_path, canonical_algo, stop_event=stop_event)
    return HashedImageEntry(path=path_str, image_hash=computed)


def find_similar_groups(
    entries: Iterable[Any] | Mapping[Any, Any],
    max_distance: int = DEFAULT_MAX_DISTANCE,
    algorithm: str = "phash",
    *,
    rotation_invariant: bool = False,
    stop_event: StopEventLike | None = None,
) -> SimilarityGroupingResult:
    """Group perceptually similar images whose Hamming distance is ``<= max_distance``.

    Bucketing Approach (Multi-Index Pigeonhole Prefix Bucketing):
        Rather than comparing all ``O(n^2)`` pairs across a large library, each
        64-bit hash is partitioned into ``B = max_distance + 1`` disjoint bit
        prefixes/slices (for example, 9 slices of 7-8 bits when ``max_distance = 8``,
        or a single 64-bit slice when ``max_distance = 0``). By the pigeonhole
        principle, any two 64-bit hashes that differ in at most ``max_distance``
        bits in total must be 100% identical in at least one of the
        ``max_distance + 1`` disjoint slices.

        Each entry is indexed into ``B`` hash tables keyed by ``(slice_index, slice_bits)``.
        Only pairs that collide in at least one bucket are candidate neighbours;
        their exact 64-bit Hamming distance is verified before merging their
        components in a Union-Find structure. Furthermore, pairs already in the
        same Union-Find connected component are skipped in ``O(alpha(n))`` time,
        avoiding redundant distance checks inside dense clusters.

    Transitivity & Determinism:
        Connected components are formed with Union-Find so similarity grouping is
        transitive (if A matches B and B matches C, {A, B, C} form one group
        regardless of entry order). Within each group of size >= 2, entries are
        sorted by path, and the returned groups are sorted by their first path.
    """
    if max_distance < 0 or max_distance > HASH_BITS:
        raise ValueError(f"max_distance must be between 0 and {HASH_BITS}, got {max_distance}.")
    canonical_algo = normalize_algorithm(algorithm)
    if canonical_algo not in SUPPORTED_ALGORITHMS:
        raise ValueError(f"Unsupported perceptual hash algorithm: {algorithm!r}.")

    if _is_cancelled(stop_event):
        return SimilarityGroupingResult(groups=(), errors=(), cancelled=True)

    raw_items: Iterable[Any]
    if isinstance(entries, Mapping):
        raw_items = list(entries.items())
    else:
        raw_items = entries

    hashed_entries: list[HashedImageEntry] = []
    errors: list[ImageAnalysisError] = []

    for item in raw_items:
        if _is_cancelled(stop_event):
            return SimilarityGroupingResult(groups=(), errors=tuple(errors), cancelled=True)
        item_path = str(item[0] if isinstance(item, tuple) and len(item) == 2 else getattr(item, "path", item))
        try:
            coerced = _coerce_entry(
                item,
                canonical_algo,
                rotation_invariant=rotation_invariant,
                stop_event=stop_event,
            )
            if _is_cancelled(stop_event):
                return SimilarityGroupingResult(groups=(), errors=tuple(errors), cancelled=True)
            hashed_entries.append(coerced)
        except ValueError as exc:
            # Propagate cross-algorithm mismatches immediately as caller contract errors
            if "does not match" in str(exc):
                raise
            errors.append(ImageAnalysisError(path=item_path, message=str(exc) or type(exc).__name__))
        except Exception as exc:
            errors.append(ImageAnalysisError(path=item_path, message=str(exc) or type(exc).__name__))

    # Sort entries deterministically by path before indexing so bucket traversal is order-invariant
    hashed_entries.sort(key=lambda e: (e.path, e.image_hash.bits))
    n_entries = len(hashed_entries)
    if n_entries < 2:
        return SimilarityGroupingResult(groups=(), errors=tuple(errors), cancelled=False)

    uf = _UnionFind(n_entries)
    bit_slices = _partition_bit_slices(HASH_BITS, max_distance)
    buckets: dict[tuple[int, int], list[int]] = {}

    for idx, entry in enumerate(hashed_entries):
        if _is_cancelled(stop_event):
            return SimilarityGroupingResult(groups=(), errors=tuple(errors), cancelled=True)
        for variant_hash in entry.all_hashes:
            bits_val = variant_hash.bits
            for slice_idx, (shift, mask) in enumerate(bit_slices):
                key = (slice_idx, (bits_val >> shift) & mask)
                bucket = buckets.get(key)
                if bucket is None:
                    buckets[key] = [idx]
                elif bucket[-1] != idx:
                    bucket.append(idx)

    for bucket in buckets.values():
        if _is_cancelled(stop_event):
            return SimilarityGroupingResult(groups=(), errors=tuple(errors), cancelled=True)
        b_len = len(bucket)
        if b_len < 2:
            continue
        for i in range(b_len):
            idx_a = bucket[i]
            entry_a = hashed_entries[idx_a]
            hashes_a = entry_a.all_hashes
            for j in range(i + 1, b_len):
                if _is_cancelled(stop_event):
                    return SimilarityGroupingResult(groups=(), errors=tuple(errors), cancelled=True)
                idx_b = bucket[j]
                if uf.find(idx_a) == uf.find(idx_b):
                    continue
                hashes_b = hashed_entries[idx_b].all_hashes
                dist = min(ha.hamming_distance(hb) for ha in hashes_a for hb in hashes_b)
                if dist <= max_distance:
                    uf.union(idx_a, idx_b)

    components: dict[int, list[HashedImageEntry]] = {}
    for idx, entry in enumerate(hashed_entries):
        root = uf.find(idx)
        components.setdefault(root, []).append(entry)

    groups: list[SimilarGroup] = []
    for comp_entries in components.values():
        if len(comp_entries) < 2:
            continue
        sorted_entries = tuple(sorted(comp_entries, key=lambda e: (e.path, e.image_hash.bits)))
        sorted_paths = tuple(e.path for e in sorted_entries)
        groups.append(
            SimilarGroup(
                paths=sorted_paths,
                entries=sorted_entries,
                algorithm=canonical_algo,
            )
        )

    groups.sort(key=lambda g: g.paths)
    return SimilarityGroupingResult(groups=tuple(groups), errors=tuple(errors), cancelled=False)


def _has_exif_capture_metadata(image: Any) -> bool:
    """Return True if ``image`` contains EXIF capture timestamp or camera make/model tags."""
    try:
        exif = image.getexif()
    except Exception:
        return False
    if not exif:
        return False

    for tag in _EXIF_CAPTURE_TAGS:
        val = exif.get(tag)
        if val is not None and str(val).strip():
            return True

    if hasattr(exif, "get_ifd"):
        try:
            sub_ifd = exif.get_ifd(_EXIF_IFD_POINTER_TAG)
            if isinstance(sub_ifd, Mapping):
                for tag in _EXIF_CAPTURE_TAGS:
                    val = sub_ifd.get(tag)
                    if val is not None and str(val).strip():
                        return True
        except Exception:
            pass
    return False


def _score_candidate(raw_candidate: Any) -> CandidateScore:
    """Extract resolution, byte size, EXIF capture presence, mtime, and path for a candidate."""
    if isinstance(raw_candidate, CandidateScore):
        return raw_candidate

    pil_image = _require_pillow()
    raw_path = getattr(raw_candidate, "path", raw_candidate)
    path_str = str(raw_path)
    candidate_path = Path(path_str)
    stat_result = candidate_path.stat()

    with pil_image.open(candidate_path) as img:
        width, height = img.size
        if width <= 0 or height <= 0:
            raise ValueError(f"Invalid image dimensions: {width}x{height}")
        has_exif = _has_exif_capture_metadata(img)

    return CandidateScore(
        path=path_str,
        width=int(width),
        height=int(height),
        pixel_count=int(width) * int(height),
        byte_size=int(stat_result.st_size),
        has_exif_capture=bool(has_exif),
        mtime=float(stat_result.st_mtime),
    )


def _explain_rejection(keeper: CandidateScore, rejected: CandidateScore) -> str:
    """Build a human-readable explanation for why ``rejected`` lost to ``keeper``."""
    reasons: list[str] = []
    if rejected.pixel_count < keeper.pixel_count:
        reasons.append(
            f"lower resolution ({rejected.width}x{rejected.height}, {rejected.pixel_count} px "
            f"vs keeper {keeper.width}x{keeper.height}, {keeper.pixel_count} px)"
        )
    elif rejected.pixel_count == keeper.pixel_count and rejected.byte_size < keeper.byte_size:
        reasons.append(
            f"smaller byte size at equal resolution indicating heavier lossy compression "
            f"({rejected.byte_size} bytes vs keeper {keeper.byte_size} bytes)"
        )

    if not rejected.has_exif_capture and keeper.has_exif_capture:
        reasons.append("missing EXIF capture metadata present on the keeper")

    if not reasons:
        if rejected.mtime > keeper.mtime:
            reasons.append(
                f"later filesystem modification time (mtime {rejected.mtime:.3f} vs keeper {keeper.mtime:.3f})"
            )
        else:
            reasons.append(
                f"deterministic full-path tie-breaker ({rejected.path!r} vs keeper {keeper.path!r})"
            )

    summary = "; ".join(reasons)
    return summary[0].upper() + summary[1:]


def recommend_keeper(
    candidates: Iterable[Any],
    *,
    stop_event: StopEventLike | None = None,
) -> KeepRecommendation:
    """Rank near-duplicate candidates and recommend the single best keeper.

    Scoring priority (highest to lowest):
        1. Pixel count (``width * height``, higher is preferred)
        2. Absence of lossy recompression artefacts (larger byte size at equal pixel count)
        3. Presence of EXIF capture metadata (``True`` preferred over ``False``)
        4. Earliest filesystem modification time (smaller ``st_mtime`` preferred)
        5. Full file path (lexicographically earliest path as final deterministic tie-breaker)

    Pure analysis only: never deletes, moves, or modifies any file.
    """
    if _is_cancelled(stop_event):
        return KeepRecommendation(keeper="", cancelled=True)

    scores: list[CandidateScore] = []
    errors: list[ImageAnalysisError] = []
    error_reasons: dict[str, str] = {}

    for item in candidates:
        if _is_cancelled(stop_event):
            return KeepRecommendation(
                keeper="",
                errors=tuple(errors),
                cancelled=True,
            )
        item_path = str(getattr(item, "path", item))
        try:
            score = _score_candidate(item)
            scores.append(score)
        except Exception as exc:
            msg = str(exc) or type(exc).__name__
            errors.append(ImageAnalysisError(path=item_path, message=msg))
            error_reasons[item_path] = f"Unreadable or corrupt image: {msg}"

    if not scores:
        return KeepRecommendation(
            keeper="",
            keeper_score=None,
            reasons=error_reasons,
            rejected=tuple(
                RejectedCandidate(path=err.path, reason=error_reasons[err.path], score=None)
                for err in errors
            ),
            rankings=(),
            errors=tuple(errors),
            cancelled=False,
        )

    ranked = tuple(sorted(scores, key=lambda s: s.sort_key()))
    keeper_score = ranked[0]
    reasons: dict[str, str] = {}
    rejected_list: list[RejectedCandidate] = []

    for candidate in ranked[1:]:
        if _is_cancelled(stop_event):
            return KeepRecommendation(
                keeper="",
                errors=tuple(errors),
                cancelled=True,
            )
        explanation = _explain_rejection(keeper_score, candidate)
        reasons[candidate.path] = explanation
        rejected_list.append(
            RejectedCandidate(
                path=candidate.path,
                reason=explanation,
                score=candidate,
            )
        )

    for err in errors:
        reason_text = error_reasons[err.path]
        reasons[err.path] = reason_text
        rejected_list.append(
            RejectedCandidate(
                path=err.path,
                reason=reason_text,
                score=None,
            )
        )

    return KeepRecommendation(
        keeper=keeper_score.path,
        keeper_score=keeper_score,
        reasons=reasons,
        rejected=tuple(rejected_list),
        rankings=ranked,
        errors=tuple(errors),
        cancelled=False,
    )
