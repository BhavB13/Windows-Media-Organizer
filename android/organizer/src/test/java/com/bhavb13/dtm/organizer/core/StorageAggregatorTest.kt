package com.bhavb13.dtm.organizer.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class StorageAggregatorTest {

    private fun item(
        id: String,
        kind: MediaKind,
        sizeBytes: Long,
        bucketId: String,
        bucketName: String
    ): MediaItem {
        return MediaItem(
            id = id,
            displayName = "item_$id",
            mimeType = if (kind == MediaKind.IMAGE) "image/jpeg" else "video/mp4",
            kind = kind,
            sizeBytes = sizeBytes,
            dateAddedMillis = 1_000L,
            dateModifiedMillis = 1_000L,
            bucketId = bucketId,
            bucketName = bucketName
        )
    }

    @Test
    fun emptyLibraryProducesZeroTotalsAndPopulatesBothMediaKinds() {
        val summary = StorageAggregator.summarize(emptyList(), emptyList())

        assertEquals(0, summary.totalCount)
        assertEquals(0L, summary.totalBytes)
        assertEquals(0, summary.imageStats.count)
        assertEquals(0L, summary.imageStats.bytes)
        assertEquals(0, summary.videoStats.count)
        assertEquals(0L, summary.videoStats.bytes)
        assertTrue(summary.byBucket.isEmpty())
        assertEquals(0, summary.duplicateGroupCount)
        assertEquals(0, summary.duplicateItemCount)
        assertEquals(0L, summary.recoverableBytes)
        assertEquals(0L, summary.netBytesAfterDeduplication)
    }

    @Test
    fun storageAggregationArithmeticByKindByBucketAndRecoverableBytes() {
        val imgCam1 = item("1", MediaKind.IMAGE, 4_000L, "b_cam", "Camera")
        val imgCam2 = item("2", MediaKind.IMAGE, 4_000L, "b_cam", "Camera")
        val vidCam1 = item("3", MediaKind.VIDEO, 20_000L, "b_cam", "Camera")
        val imgDl1 = item("4", MediaKind.IMAGE, 4_000L, "b_dl", "Download")
        val imgShot1 = item("5", MediaKind.IMAGE, 1_500L, "b_shot", "Screenshots")
        val vidMov1 = item("6", MediaKind.VIDEO, 50_000L, "b_mov", "Movies")
        val vidMov2 = item("7", MediaKind.VIDEO, 50_000L, "b_mov", "Movies")

        val allItems = listOf(imgCam1, imgCam2, vidCam1, imgDl1, imgShot1, vidMov1, vidMov2)

        // Duplicate group 1: imgCam1 (keeper) + [imgCam2, imgDl1] -> 8,000 recoverable bytes
        val group1 = DuplicateGroup(
            sha256 = "a".repeat(64),
            keeper = imgCam1,
            duplicates = listOf(imgCam2, imgDl1),
            reason = "Primary copy"
        )
        // Duplicate group 2: vidMov1 (keeper) + [vidMov2] -> 50,000 recoverable bytes
        val group2 = DuplicateGroup(
            sha256 = "b".repeat(64),
            keeper = vidMov1,
            duplicates = listOf(vidMov2),
            reason = "Primary video"
        )

        val summary = StorageAggregator.summarize(allItems, listOf(group1, group2))

        // Total: 7 items, 4,000*3 + 20,000 + 1,500 + 50,000*2 = 133,500 bytes
        assertEquals(7, summary.totalCount)
        assertEquals(133_500L, summary.totalBytes)

        // By kind:
        // Images: 4 items, 4,000*3 + 1,500 = 13,500 bytes
        // Videos: 3 items, 20,000 + 50,000*2 = 120,000 bytes
        assertEquals(4, summary.imageStats.count)
        assertEquals(13_500L, summary.imageStats.bytes)
        assertEquals(3, summary.videoStats.count)
        assertEquals(120_000L, summary.videoStats.bytes)

        // By bucket (sorted by bytes descending):
        // 1. Movies: 2 videos, 100,000 bytes
        // 2. Camera: 2 images + 1 video = 3 items, 28,000 bytes
        // 3. Download: 1 image, 4,000 bytes
        // 4. Screenshots: 1 image, 1,500 bytes
        assertEquals(4, summary.byBucket.size)

        assertEquals("Movies", summary.byBucket[0].bucketName)
        assertEquals(2, summary.byBucket[0].count)
        assertEquals(0, summary.byBucket[0].imageCount)
        assertEquals(2, summary.byBucket[0].videoCount)
        assertEquals(100_000L, summary.byBucket[0].bytes)

        assertEquals("Camera", summary.byBucket[1].bucketName)
        assertEquals(3, summary.byBucket[1].count)
        assertEquals(2, summary.byBucket[1].imageCount)
        assertEquals(1, summary.byBucket[1].videoCount)
        assertEquals(28_000L, summary.byBucket[1].bytes)

        assertEquals("Download", summary.byBucket[2].bucketName)
        assertEquals(1, summary.byBucket[2].count)
        assertEquals(4_000L, summary.byBucket[2].bytes)

        assertEquals("Screenshots", summary.byBucket[3].bucketName)
        assertEquals(1, summary.byBucket[3].count)
        assertEquals(1_500L, summary.byBucket[3].bytes)

        // Recoverable bytes: 8,000 + 50,000 = 58,000 bytes across 3 redundant files in 2 groups
        assertEquals(2, summary.duplicateGroupCount)
        assertEquals(3, summary.duplicateItemCount)
        assertEquals(58_000L, summary.recoverableBytes)
        assertEquals(75_500L, summary.netBytesAfterDeduplication)
    }
}
