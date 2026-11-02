package com.bhavb13.dtm.organizer.core

/**
 * The one ordering used to page MediaStore results.
 *
 * Each collection (images, videos) is read from MediaStore already truncated to
 * the first `offset + limit` rows, so the order used to merge and slice them
 * must be the same order MediaStore truncated in. If it is not, an item ranked
 * just past the truncation point can belong on an earlier page: it is left out
 * of that page, then sorts ahead of a later page's offset and is never returned
 * at all. Sorting the merged window by capture date after truncating by
 * modification date did exactly that.
 *
 * The order is modification time, newest first, then MediaStore row id, newest
 * first - the same as the repository's SQL `date_modified DESC, _id DESC`.
 * Kind only breaks ties between the two collections, whose row ids are
 * independent.
 */
object MediaPaging {

    /** The SQL ORDER BY that must stay in step with [ORDER]. */
    const val SQL_SORT_ORDER = "date_modified DESC, _id DESC"

    /** MediaStore row id, the last path segment of the content URI. */
    fun rowId(item: MediaItem): Long = item.id.substringAfterLast('/').toLongOrNull() ?: 0L

    val ORDER: Comparator<MediaItem> =
        compareByDescending<MediaItem> { it.dateModifiedMillis / 1000L }
            .thenByDescending { rowId(it) }
            .thenBy { it.kind.ordinal }

    /** Merge collections that were each truncated in [ORDER], then take one page. */
    fun page(collections: List<List<MediaItem>>, offset: Int, limit: Int): List<MediaItem> {
        if (offset < 0 || limit <= 0) return emptyList()
        val merged = collections.flatten().sortedWith(ORDER)
        if (offset >= merged.size) return emptyList()
        return merged.subList(offset, (offset + limit).coerceAtMost(merged.size))
    }
}
