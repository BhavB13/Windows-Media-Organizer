package com.bhavb13.dtm.organizer.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class MediaPagingTest {

    private fun item(kind: MediaKind, rowId: Long, modifiedSec: Long, takenMillis: Long?): MediaItem {
        val collection = if (kind == MediaKind.IMAGE) "images" else "video"
        return MediaItem(
            id = "content://media/external/$collection/media/$rowId",
            displayName = "$collection-$rowId",
            mimeType = if (kind == MediaKind.IMAGE) "image/jpeg" else "video/mp4",
            kind = kind,
            sizeBytes = 100L,
            dateAddedMillis = modifiedSec * 1000L,
            dateModifiedMillis = modifiedSec * 1000L,
            captureDateMillis = takenMillis,
        )
    }

    /** What MediaStore returns for a collection: its rows in SQL order, truncated. */
    private fun query(rows: List<MediaItem>, maxCount: Int): List<MediaItem> =
        rows.sortedWith(MediaPaging.ORDER).take(maxCount)

    private fun pageThroughEverything(images: List<MediaItem>, videos: List<MediaItem>, pageSize: Int): List<MediaItem> {
        val seen = mutableListOf<MediaItem>()
        var offset = 0
        while (true) {
            val window = offset + pageSize
            val page = MediaPaging.page(listOf(query(images, window), query(videos, window)), offset, pageSize)
            if (page.isEmpty()) break
            seen += page
            offset += page.size
        }
        return seen
    }

    @Test
    fun everyItemIsReturnedExactlyOnceWhenCaptureAndModifiedDatesDisagree() {
        // 61 images whose capture dates run opposite to their modification dates:
        // the oldest-modified is the most recently taken. Sorting the truncated
        // window by capture date used to drop it from every page.
        val images = (1L..61L).map { item(MediaKind.IMAGE, it, modifiedSec = 1_000L + it, takenMillis = (10_000L - it) * 1000L) }
        val videos = (1L..7L).map { item(MediaKind.VIDEO, it, modifiedSec = 1_030L + it, takenMillis = null) }

        val seen = pageThroughEverything(images, videos, pageSize = 30)

        assertEquals(68, seen.size)
        assertEquals((images + videos).map { it.id }.toSet(), seen.map { it.id }.toSet())
    }

    @Test
    fun ties_on_modified_time_are_broken_by_numeric_row_id_not_text() {
        // As text "1000" sorts before "999"; as MediaStore row ids it is newer.
        val a = item(MediaKind.IMAGE, 999L, modifiedSec = 5L, takenMillis = null)
        val b = item(MediaKind.IMAGE, 1000L, modifiedSec = 5L, takenMillis = null)
        assertEquals(listOf(b, a), listOf(a, b).sortedWith(MediaPaging.ORDER))
    }

    @Test
    fun pagesNeverOverlap() {
        val images = (1L..25L).map { item(MediaKind.IMAGE, it, modifiedSec = it % 4, takenMillis = it * 7) }
        val seen = pageThroughEverything(images, emptyList(), pageSize = 4)
        assertEquals(seen.size, seen.map { it.id }.toSet().size)
        assertTrue(seen.size == 25)
    }

    @Test
    fun outOfRangeRequestsReturnNothing() {
        val images = listOf(item(MediaKind.IMAGE, 1L, 1L, null))
        assertTrue(MediaPaging.page(listOf(images), offset = 5, limit = 10).isEmpty())
        assertTrue(MediaPaging.page(listOf(images), offset = -1, limit = 10).isEmpty())
        assertTrue(MediaPaging.page(listOf(images), offset = 0, limit = 0).isEmpty())
    }
}
