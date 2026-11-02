package com.bhavb13.dtm.organizer.core

/**
 * Search and filter criteria over the media library.
 *
 * Predicates supported:
 * - [filenameQuery]: literal case-insensitive substring on [MediaItem.displayName]. Safe against
 *   regex metacharacters (`.*+?^${}()|[]\`).
 * - [mediaKind]: exact match on [MediaItem.kind] (`IMAGE` or `VIDEO`), or `null` for all kinds.
 * - [minSizeBytes] / [maxSizeBytes]: inclusive bounds on [MediaItem.sizeBytes].
 * - [minDateMillis] / [maxDateMillis]: inclusive bounds on [MediaItem.effectiveCaptureDateMillis].
 * - [bucketId]: optional exact match on [MediaItem.bucketId], or `null` for all buckets.
 */
data class MediaFilterCriteria(
    val filenameQuery: String = "",
    val mediaKind: MediaKind? = null,
    val minSizeBytes: Long? = null,
    val maxSizeBytes: Long? = null,
    val minDateMillis: Long? = null,
    val maxDateMillis: Long? = null,
    val bucketId: String? = null
) {
    val isDefault: Boolean
        get() = filenameQuery.isBlank() &&
            mediaKind == null &&
            minSizeBytes == null &&
            maxSizeBytes == null &&
            minDateMillis == null &&
            maxDateMillis == null &&
            bucketId.isNullOrBlank()
}

/**
 * Pure Kotlin predicate evaluator for [MediaFilterCriteria].
 */
object MediaFilter {

    fun matches(item: MediaItem, criteria: MediaFilterCriteria): Boolean {
        // 1. Filename substring predicate (literal substring, never regex)
        val rawQuery = criteria.filenameQuery.trim()
        if (rawQuery.isNotEmpty()) {
            if (!item.displayName.contains(rawQuery, ignoreCase = true)) {
                return false
            }
        }

        // 2. Media kind predicate
        if (criteria.mediaKind != null && item.kind != criteria.mediaKind) {
            return false
        }

        // 3. Size range predicate (inclusive)
        if (criteria.minSizeBytes != null && item.sizeBytes < criteria.minSizeBytes) {
            return false
        }
        if (criteria.maxSizeBytes != null && item.sizeBytes > criteria.maxSizeBytes) {
            return false
        }

        // 4. Date range predicate (inclusive on effectiveCaptureDateMillis)
        val itemDate = item.effectiveCaptureDateMillis
        if (criteria.minDateMillis != null && itemDate < criteria.minDateMillis) {
            return false
        }
        if (criteria.maxDateMillis != null && itemDate > criteria.maxDateMillis) {
            return false
        }

        // 5. Optional bucket filter
        if (!criteria.bucketId.isNullOrBlank() && item.bucketId != criteria.bucketId) {
            return false
        }

        return true
    }

    fun filter(items: Iterable<MediaItem>, criteria: MediaFilterCriteria): List<MediaItem> {
        return items.filter { matches(it, criteria) }
    }
}
