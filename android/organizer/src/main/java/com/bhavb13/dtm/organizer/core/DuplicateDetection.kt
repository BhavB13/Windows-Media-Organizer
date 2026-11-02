package com.bhavb13.dtm.organizer.core

import java.util.Locale

/**
 * Result of ranking a set of content-identical [MediaItem]s to recommend which copy to keep.
 */
data class KeeperChoice(
    val keeper: MediaItem,
    val duplicates: List<MediaItem>,
    val reason: String
)

/**
 * A group of two or more files whose full-file SHA-256 digests are identical.
 */
data class DuplicateGroup(
    val sha256: String,
    val keeper: MediaItem,
    val duplicates: List<MediaItem>,
    val reason: String
) {
    init {
        require(duplicates.isNotEmpty()) { "A DuplicateGroup must contain at least one duplicate beyond the keeper." }
    }

    val allItems: List<MediaItem>
        get() = listOf(keeper) + duplicates

    val copyCount: Int
        get() = 1 + duplicates.size

    val totalBytes: Long
        get() = allItems.sumOf { it.sizeBytes.coerceAtLeast(0L) }

    /**
     * Bytes that could be recovered by keeping [keeper] and resolving [duplicates].
     */
    val recoverableBytes: Long
        get() = duplicates.sumOf { it.sizeBytes.coerceAtLeast(0L) }
}

/**
 * Records a file that could not be opened or hashed during a duplicate scan.
 */
data class FailedHashItem(
    val item: MediaItem,
    val message: String
)

/**
 * Determinate progress snapshot emitted during candidate hashing.
 */
data class DuplicateScanProgress(
    val phase: String,
    val completedItems: Int,
    val totalItems: Int,
    val currentItemName: String
) {
    val fraction: Float
        get() = if (totalItems <= 0) 0f else (completedItems.toFloat() / totalItems.toFloat()).coerceIn(0f, 1f)
}

/**
 * Complete result of a read-only content-identity duplicate scan.
 */
data class DuplicateScanResult(
    val groups: List<DuplicateGroup>,
    val scannedItemCount: Int,
    val hashedCandidateCount: Int,
    val skippedUniqueSizeCount: Int,
    val failures: List<FailedHashItem>,
    val wasCancelled: Boolean
) {
    val totalGroupCount: Int
        get() = groups.size

    val totalDuplicateCount: Int
        get() = groups.sumOf { it.duplicates.size }

    val totalRecoverableBytes: Long
        get() = groups.sumOf { it.recoverableBytes }

    companion object {
        fun cancelled(scannedItemCount: Int = 0): DuplicateScanResult {
            return DuplicateScanResult(
                groups = emptyList(),
                scannedItemCount = scannedItemCount,
                hashedCandidateCount = 0,
                skippedUniqueSizeCount = 0,
                failures = emptyList(),
                wasCancelled = true
            )
        }
    }
}

/**
 * Read-only report summarizing exact duplicate findings. No media files are deleted, moved, or
 * modified by generating or viewing this report.
 */
data class DuplicateReport(
    val generatedAtMillis: Long,
    val safetyNotice: String,
    val scannedItemCount: Int,
    val groupCount: Int,
    val duplicateFileCount: Int,
    val totalRecoverableBytes: Long,
    val groups: List<DuplicateGroup>,
    val formattedText: String
)

/**
 * Deterministic keep-recommendation algorithm for identical-content media files.
 *
 * Ranking order (highest priority first):
 * 1. Higher resolution (`width * height` pixel count descending).
 * 2. Presence of camera make/model metadata.
 * 3. Primary camera bucket (`Camera` or `DCIM`) over secondary/download buckets.
 * 4. Earliest positive capture/effective timestamp (preferring original over later copies).
 * 5. Shorter, cleaner filename (e.g. `IMG_0001.jpg` over `IMG_0001 (1).jpg`), then lexicographical name.
 * 6. Deterministic final tie-breaker: `id` ascending.
 */
object KeepRecommender {
    private val PRIMARY_CAMERA_BUCKETS = setOf("camera", "dcim", "100andro", "100media")

    private fun isPrimaryBucket(bucketName: String): Boolean {
        val norm = bucketName.trim().lowercase(Locale.ROOT)
        return norm in PRIMARY_CAMERA_BUCKETS || norm.startsWith("dcim/")
    }

    /**
     * Comparator where the "best" keeper sorts first (index 0).
     */
    val keeperComparator: Comparator<MediaItem> = Comparator { a, b ->
        // 1. Higher pixel count wins
        val pixelCmp = b.pixelCount.compareTo(a.pixelCount)
        if (pixelCmp != 0) return@Comparator pixelCmp

        // 2. Camera metadata presence wins
        val camCmp = b.hasCameraMetadata.compareTo(a.hasCameraMetadata)
        if (camCmp != 0) return@Comparator camCmp

        // 3. Primary camera bucket wins
        val bucketCmp = isPrimaryBucket(b.bucketName).compareTo(isPrimaryBucket(a.bucketName))
        if (bucketCmp != 0) return@Comparator bucketCmp

        // 4. Earliest positive capture date wins
        val aDate = a.effectiveCaptureDateMillis
        val bDate = b.effectiveCaptureDateMillis
        val aHasDate = aDate > 0L
        val bHasDate = bDate > 0L
        if (aHasDate != bHasDate) {
            return@Comparator bHasDate.compareTo(aHasDate)
        }
        if (aHasDate && bHasDate && aDate != bDate) {
            return@Comparator aDate.compareTo(bDate)
        }

        // 5. Shorter display name (e.g. "IMG_01.jpg" before "IMG_01 (1).jpg"), then alphabetical
        val lenCmp = a.displayName.length.compareTo(b.displayName.length)
        if (lenCmp != 0) return@Comparator lenCmp

        val nameCmp = a.displayName.compareTo(b.displayName)
        if (nameCmp != 0) return@Comparator nameCmp

        // 6. Deterministic tie-breaker by unique ID
        a.id.compareTo(b.id)
    }

    fun chooseKeeper(items: List<MediaItem>): KeeperChoice {
        require(items.size >= 2) { "KeepRecommender requires at least 2 items." }
        val sorted = items.sortedWith(keeperComparator)
        val keeper = sorted.first()
        val duplicates = sorted.drop(1)
        val reason = buildReason(keeper, duplicates, sorted.size)
        return KeeperChoice(keeper = keeper, duplicates = duplicates, reason = reason)
    }

    private fun buildReason(keeper: MediaItem, duplicates: List<MediaItem>, totalCount: Int): String {
        val maxOtherPixels = duplicates.maxOfOrNull { it.pixelCount } ?: 0L
        if (keeper.pixelCount > maxOtherPixels && keeper.hasDimensions) {
            return "Recommended keeper: highest resolution (${keeper.dimensionsLabel}) among $totalCount identical copies."
        }

        val othersHaveCamera = duplicates.any { it.hasCameraMetadata }
        if (keeper.hasCameraMetadata && !othersHaveCamera) {
            return "Recommended keeper: preserves camera metadata (${keeper.cameraSummary}) across $totalCount identical copies."
        }

        val othersInPrimary = duplicates.any { isPrimaryBucket(it.bucketName) }
        if (isPrimaryBucket(keeper.bucketName) && !othersInPrimary) {
            return "Recommended keeper: located in primary camera folder (${keeper.bucketName}) among $totalCount identical copies."
        }

        val keeperDate = keeper.effectiveCaptureDateMillis
        val earliestOtherDate = duplicates.map { it.effectiveCaptureDateMillis }.filter { it > 0L }.minOrNull()
        if (keeperDate > 0L && (earliestOtherDate == null || keeperDate < earliestOtherDate)) {
            return "Recommended keeper: earliest capture date in ${keeper.bucketName} among $totalCount identical copies."
        }

        val minOtherNameLen = duplicates.minOfOrNull { it.displayName.length } ?: Int.MAX_VALUE
        if (keeper.displayName.length < minOtherNameLen) {
            return "Recommended keeper: cleanest original filename (${keeper.displayName}) among $totalCount identical copies."
        }

        return "Recommended keeper: primary copy (${keeper.displayName} in ${keeper.bucketName}) selected deterministically among $totalCount identical copies with matching SHA-256."
    }
}

/**
 * Content-based duplicate detector.
 *
 * Safety contract:
 * - File identity is decided ONLY by the full-file SHA-256 digest.
 * - Names, timestamps, and buckets never decide a duplicate.
 * - Byte size is used solely as a cheap pre-filter before full-content hashing: files whose byte
 *   size is unique in the library cannot share content with any other file and skip hashing, while
 *   same-size files are always hashed in full to distinguish true duplicates from same-size
 *   collisions.
 */
object DuplicateDetector {
    const val READ_ONLY_SAFETY_NOTICE =
        "READ-ONLY MVP REPORT: No files have been deleted, moved, or modified. Use this report to review duplicate copies safely."

    fun findDuplicates(
        items: List<MediaItem>,
        source: ContentHashSource,
        onProgress: (DuplicateScanProgress) -> Unit = {},
        isCancelled: () -> Boolean = { false }
    ): DuplicateScanResult {
        if (isCancelled()) {
            return DuplicateScanResult.cancelled(scannedItemCount = items.size)
        }

        onProgress(
            DuplicateScanProgress(
                phase = "Pre-filtering by file size",
                completedItems = 0,
                totalItems = items.size,
                currentItemName = ""
            )
        )

        // Group by byte size as a fast pre-filter. Unique sizes cannot have identical byte streams.
        val sizeBuckets = items
            .filter { it.sizeBytes >= 0L }
            .groupBy { it.sizeBytes }

        var skippedUniqueSizeCount = 0
        val candidatesToHash = mutableListOf<MediaItem>()

        for ((_, bucket) in sizeBuckets) {
            if (bucket.size < 2) {
                skippedUniqueSizeCount += bucket.size
            } else {
                candidatesToHash.addAll(bucket)
            }
        }

        // Sort candidates deterministically so progress reporting and test execution are stable.
        candidatesToHash.sortWith(compareBy<MediaItem> { it.sizeBytes }.thenBy { it.id })

        val totalCandidates = candidatesToHash.size
        val itemsByHash = linkedMapOf<String, MutableList<MediaItem>>()
        val failures = mutableListOf<FailedHashItem>()

        for ((index, candidate) in candidatesToHash.withIndex()) {
            if (isCancelled()) {
                return DuplicateScanResult.cancelled(scannedItemCount = items.size)
            }

            onProgress(
                DuplicateScanProgress(
                    phase = "Hashing file contents (SHA-256)",
                    completedItems = index,
                    totalItems = totalCandidates,
                    currentItemName = candidate.displayName
                )
            )

            try {
                val digest = source.openStream(candidate).use { stream ->
                    Sha256Hasher.hashStream(stream, isCancelled)
                }
                if (digest == null) {
                    return DuplicateScanResult.cancelled(scannedItemCount = items.size)
                }
                itemsByHash.getOrPut(digest) { mutableListOf() }.add(candidate)
            } catch (e: Exception) {
                failures.add(
                    FailedHashItem(
                        item = candidate,
                        message = e.message ?: "Could not read file content for SHA-256 hashing"
                    )
                )
            }
        }

        if (isCancelled()) {
            return DuplicateScanResult.cancelled(scannedItemCount = items.size)
        }

        onProgress(
            DuplicateScanProgress(
                phase = "Grouping exact SHA-256 matches",
                completedItems = totalCandidates,
                totalItems = totalCandidates,
                currentItemName = ""
            )
        )

        val duplicateGroups = itemsByHash.entries
            .filter { (_, matchedItems) -> matchedItems.size >= 2 }
            .map { (sha256, matchedItems) ->
                val choice = KeepRecommender.chooseKeeper(matchedItems)
                DuplicateGroup(
                    sha256 = sha256,
                    keeper = choice.keeper,
                    duplicates = choice.duplicates,
                    reason = choice.reason
                )
            }
            .sortedWith(
                compareByDescending<DuplicateGroup> { it.recoverableBytes }
                    .thenBy { it.sha256 }
            )

        return DuplicateScanResult(
            groups = duplicateGroups,
            scannedItemCount = items.size,
            hashedCandidateCount = totalCandidates,
            skippedUniqueSizeCount = skippedUniqueSizeCount,
            failures = failures,
            wasCancelled = false
        )
    }

    /**
     * Generates a read-only summary report from a [DuplicateScanResult].
     */
    fun buildReadOnlyReport(
        scanResult: DuplicateScanResult,
        generatedAtMillis: Long = System.currentTimeMillis()
    ): DuplicateReport {
        val sb = StringBuilder()
        sb.appendLine("=== Media Organizer Duplicate Review Report (Read-Only) ===")
        sb.appendLine(READ_ONLY_SAFETY_NOTICE)
        sb.appendLine()
        sb.appendLine("Items scanned: ${scanResult.scannedItemCount}")
        sb.appendLine("Same-size candidates hashed (SHA-256): ${scanResult.hashedCandidateCount}")
        sb.appendLine("Exact duplicate groups: ${scanResult.totalGroupCount}")
        sb.appendLine("Redundant duplicate copies: ${scanResult.totalDuplicateCount}")
        sb.appendLine(
            "Potential recoverable storage: ${ByteFormatting.formatBytes(scanResult.totalRecoverableBytes)} " +
                "(${scanResult.totalRecoverableBytes} bytes)"
        )
        if (scanResult.failures.isNotEmpty()) {
            sb.appendLine("Unreadable files skipped: ${scanResult.failures.size}")
        }
        sb.appendLine()

        if (scanResult.groups.isEmpty()) {
            sb.appendLine("No exact content duplicates (SHA-256) were found in the visible media library.")
        } else {
            for ((idx, group) in scanResult.groups.withIndex()) {
                sb.appendLine("Group #${idx + 1} — SHA-256: ${group.sha256}")
                sb.appendLine(
                    "  Recoverable: ${ByteFormatting.formatBytes(group.recoverableBytes)} " +
                        "(${group.recoverableBytes} bytes across ${group.duplicates.size} redundant copy/copies)"
                )
                sb.appendLine(
                    "  [KEEP] ${group.keeper.displayName} (${group.keeper.bucketName}, " +
                        "${group.keeper.dimensionsLabel}, ${ByteFormatting.formatBytes(group.keeper.sizeBytes)})"
                )
                sb.appendLine("  Reason: ${group.reason}")
                for (dup in group.duplicates) {
                    sb.appendLine(
                        "  [DUPLICATE] ${dup.displayName} (${dup.bucketName}, " +
                            "${dup.dimensionsLabel}, ${ByteFormatting.formatBytes(dup.sizeBytes)})"
                    )
                }
                sb.appendLine()
            }
        }

        return DuplicateReport(
            generatedAtMillis = generatedAtMillis,
            safetyNotice = READ_ONLY_SAFETY_NOTICE,
            scannedItemCount = scanResult.scannedItemCount,
            groupCount = scanResult.totalGroupCount,
            duplicateFileCount = scanResult.totalDuplicateCount,
            totalRecoverableBytes = scanResult.totalRecoverableBytes,
            groups = scanResult.groups,
            formattedText = sb.toString().trimEnd()
        )
    }
}
