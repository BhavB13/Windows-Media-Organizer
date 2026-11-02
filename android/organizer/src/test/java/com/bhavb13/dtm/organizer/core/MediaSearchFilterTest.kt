package com.bhavb13.dtm.organizer.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class MediaSearchFilterTest {

    private val items = listOf(
        MediaItem(
            id = "1",
            displayName = "IMG_20260501_Holiday.jpg",
            mimeType = "image/jpeg",
            kind = MediaKind.IMAGE,
            sizeBytes = 2_000_000L,
            dateAddedMillis = 1_700_000_000_000L,
            dateModifiedMillis = 1_700_000_000_000L,
            captureDateMillis = 1_700_000_000_000L,
            bucketId = "b_camera",
            bucketName = "Camera"
        ),
        MediaItem(
            id = "2",
            displayName = "VID_20260502_Clip (1) [HD].mp4",
            mimeType = "video/mp4",
            kind = MediaKind.VIDEO,
            sizeBytes = 50_000_000L,
            dateAddedMillis = 1_710_000_000_000L,
            dateModifiedMillis = 1_710_000_000_000L,
            captureDateMillis = 1_710_000_000_000L,
            bucketId = "b_movies",
            bucketName = "Movies"
        ),
        MediaItem(
            id = "3",
            displayName = "receipt_[2026]_.*+?^\${}()|\\_final.png",
            mimeType = "image/png",
            kind = MediaKind.IMAGE,
            sizeBytes = 150_000L,
            dateAddedMillis = 1_720_000_000_000L,
            dateModifiedMillis = 1_720_000_000_000L,
            captureDateMillis = 1_720_000_000_000L,
            bucketId = "b_screenshots",
            bucketName = "Screenshots"
        )
    )

    @Test
    fun emptyAndWhitespaceQueriesMatchAllItems() {
        val emptyResult = MediaFilter.filter(items, MediaFilterCriteria(filenameQuery = ""))
        assertEquals(3, emptyResult.size)

        val blankResult = MediaFilter.filter(items, MediaFilterCriteria(filenameQuery = "   \t  "))
        assertEquals(3, blankResult.size)
    }

    @Test
    fun filenameSubstringMatchesCaseInsensitively() {
        val holiday = MediaFilter.filter(items, MediaFilterCriteria(filenameQuery = "hOLiDaY"))
        assertEquals(listOf("1"), holiday.map { it.id })

        val mp4 = MediaFilter.filter(items, MediaFilterCriteria(filenameQuery = ".MP4"))
        assertEquals(listOf("2"), mp4.map { it.id })
    }

    @Test
    fun queryWithRegexMetacharactersIsEvaluatedAsLiteralSubstring() {
        // Contains unclosed/special regex tokens: [2026], .*, +, ?, ^, $, {}, (), |, \
        // If evaluated as a regex, this would either throw PatternSyntaxException or match everything ("1", "2", "3").
        val metaQuery = "[2026]_.*+?^\${}()|\\"
        val matched = MediaFilter.filter(items, MediaFilterCriteria(filenameQuery = metaQuery))
        assertEquals(listOf("3"), matched.map { it.id })

        // ".*" must NOT act as a wildcard matching "IMG_20260501_Holiday.jpg"
        val dotStar = MediaFilter.filter(items, MediaFilterCriteria(filenameQuery = ".*"))
        assertEquals(listOf("3"), dotStar.map { it.id })

        // Unbalanced bracket "[HD" must not throw PatternSyntaxException and must match item 2
        val bracket = MediaFilter.filter(items, MediaFilterCriteria(filenameQuery = "[hd"))
        assertEquals(listOf("2"), bracket.map { it.id })
    }

    @Test
    fun mediaKindPredicateFiltersImagesAndVideos() {
        val onlyImages = MediaFilter.filter(items, MediaFilterCriteria(mediaKind = MediaKind.IMAGE))
        assertEquals(listOf("1", "3"), onlyImages.map { it.id })

        val onlyVideos = MediaFilter.filter(items, MediaFilterCriteria(mediaKind = MediaKind.VIDEO))
        assertEquals(listOf("2"), onlyVideos.map { it.id })

        val allKinds = MediaFilter.filter(items, MediaFilterCriteria(mediaKind = null))
        assertEquals(listOf("1", "2", "3"), allKinds.map { it.id })
    }

    @Test
    fun sizeRangePredicateHonorsInclusiveBounds() {
        // Exact boundary match on 150,000 to 2,000,000
        val medium = MediaFilter.filter(
            items,
            MediaFilterCriteria(minSizeBytes = 150_000L, maxSizeBytes = 2_000_000L)
        )
        assertEquals(listOf("1", "3"), medium.map { it.id })

        val largeOnly = MediaFilter.filter(
            items,
            MediaFilterCriteria(minSizeBytes = 10_000_000L, maxSizeBytes = null)
        )
        assertEquals(listOf("2"), largeOnly.map { it.id })

        val smallOnly = MediaFilter.filter(
            items,
            MediaFilterCriteria(minSizeBytes = null, maxSizeBytes = 500_000L)
        )
        assertEquals(listOf("3"), smallOnly.map { it.id })
    }

    @Test
    fun dateRangePredicateHonorsInclusiveBounds() {
        val midRange = MediaFilter.filter(
            items,
            MediaFilterCriteria(
                minDateMillis = 1_705_000_000_000L,
                maxDateMillis = 1_715_000_000_000L
            )
        )
        assertEquals(listOf("2"), midRange.map { it.id })

        // Exact boundary check
        val exact = MediaFilter.filter(
            items,
            MediaFilterCriteria(
                minDateMillis = 1_700_000_000_000L,
                maxDateMillis = 1_710_000_000_000L
            )
        )
        assertEquals(listOf("1", "2"), exact.map { it.id })
    }

    @Test
    fun bucketIdAndCombinedPredicatesFilterSimultaneously() {
        val criteria = MediaFilterCriteria(
            filenameQuery = "receipt",
            mediaKind = MediaKind.IMAGE,
            minSizeBytes = 100_000L,
            maxSizeBytes = 200_000L,
            minDateMillis = 1_715_000_000_000L,
            maxDateMillis = 1_725_000_000_000L,
            bucketId = "b_screenshots"
        )
        assertFalse(criteria.isDefault)
        val matched = MediaFilter.filter(items, criteria)
        assertEquals(listOf("3"), matched.map { it.id })

        val mismatchedBucket = criteria.copy(bucketId = "b_camera")
        assertTrue(MediaFilter.filter(items, mismatchedBucket).isEmpty())
    }
}
