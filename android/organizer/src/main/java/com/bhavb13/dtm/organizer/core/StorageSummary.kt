package com.bhavb13.dtm.organizer.core

/**
 * Count and byte totals for a single [MediaKind].
 */
data class KindStorageStats(
    val kind: MediaKind,
    val count: Int,
    val bytes: Long
)

/**
 * Count and byte totals for a single MediaStore bucket / folder.
 */
data class BucketStorageStats(
    val bucketId: String,
    val bucketName: String,
    val count: Int,
    val bytes: Long,
    val imageCount: Int,
    val videoCount: Int
)

/**
 * Aggregated storage breakdown across the library by media kind, by bucket, and by recoverable
 * exact-duplicate bytes.
 */
data class StorageSummary(
    val totalCount: Int,
    val totalBytes: Long,
    val byKind: Map<MediaKind, KindStorageStats>,
    val byBucket: List<BucketStorageStats>,
    val duplicateGroupCount: Int,
    val duplicateItemCount: Int,
    val recoverableBytes: Long
) {
    val imageStats: KindStorageStats
        get() = byKind[MediaKind.IMAGE] ?: KindStorageStats(MediaKind.IMAGE, 0, 0L)

    val videoStats: KindStorageStats
        get() = byKind[MediaKind.VIDEO] ?: KindStorageStats(MediaKind.VIDEO, 0, 0L)

    val netBytesAfterDeduplication: Long
        get() = (totalBytes - recoverableBytes).coerceAtLeast(0L)
}

/**
 * Pure Kotlin storage summary calculator.
 */
object StorageAggregator {

    fun summarize(
        items: Iterable<MediaItem>,
        duplicateGroups: Iterable<DuplicateGroup> = emptyList()
    ): StorageSummary {
        var totalCount = 0
        var totalBytes = 0L

        var imageCount = 0
        var imageBytes = 0L
        var videoCount = 0
        var videoBytes = 0L

        class MutableBucket(val bucketId: String, val bucketName: String) {
            var count: Int = 0
            var bytes: Long = 0L
            var images: Int = 0
            var videos: Int = 0
        }

        val bucketMap = linkedMapOf<String, MutableBucket>()

        for (item in items) {
            val safeSize = item.sizeBytes.coerceAtLeast(0L)
            totalCount += 1
            totalBytes += safeSize

            when (item.kind) {
                MediaKind.IMAGE -> {
                    imageCount += 1
                    imageBytes += safeSize
                }
                MediaKind.VIDEO -> {
                    videoCount += 1
                    videoBytes += safeSize
                }
            }

            val resolvedBucketName = item.bucketName.ifBlank { "Uncategorized" }
            val key = if (item.bucketId.isNotBlank()) {
                item.bucketId
            } else {
                "name:$resolvedBucketName"
            }
            val bucket = bucketMap.getOrPut(key) {
                MutableBucket(
                    bucketId = item.bucketId.ifBlank { resolvedBucketName },
                    bucketName = resolvedBucketName
                )
            }
            bucket.count += 1
            bucket.bytes += safeSize
            if (item.kind == MediaKind.IMAGE) {
                bucket.images += 1
            } else {
                bucket.videos += 1
            }
        }

        val byKind = mapOf(
            MediaKind.IMAGE to KindStorageStats(MediaKind.IMAGE, imageCount, imageBytes),
            MediaKind.VIDEO to KindStorageStats(MediaKind.VIDEO, videoCount, videoBytes)
        )

        val byBucket = bucketMap.values
            .map { b ->
                BucketStorageStats(
                    bucketId = b.bucketId,
                    bucketName = b.bucketName,
                    count = b.count,
                    bytes = b.bytes,
                    imageCount = b.images,
                    videoCount = b.videos
                )
            }
            .sortedWith(
                compareByDescending<BucketStorageStats> { it.bytes }
                    .thenBy { it.bucketName }
                    .thenBy { it.bucketId }
            )

        var duplicateGroupCount = 0
        var duplicateItemCount = 0
        var recoverableBytes = 0L

        for (group in duplicateGroups) {
            duplicateGroupCount += 1
            duplicateItemCount += group.duplicates.size
            recoverableBytes += group.recoverableBytes
        }

        return StorageSummary(
            totalCount = totalCount,
            totalBytes = totalBytes,
            byKind = byKind,
            byBucket = byBucket,
            duplicateGroupCount = duplicateGroupCount,
            duplicateItemCount = duplicateItemCount,
            recoverableBytes = recoverableBytes
        )
    }
}
