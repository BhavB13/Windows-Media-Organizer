package com.bhavb13.dtm.organizer.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException
import java.io.InputStream
import java.util.concurrent.atomic.AtomicInteger

class DuplicateDetectionTest {

    private fun sampleItem(
        id: String,
        displayName: String = "IMG_$id.jpg",
        kind: MediaKind = MediaKind.IMAGE,
        sizeBytes: Long = 1024L,
        captureDateMillis: Long? = 1_700_000_000_000L,
        width: Int? = 1920,
        height: Int? = 1080,
        cameraMake: String? = null,
        cameraModel: String? = null,
        bucketId: String = "bucket_cam",
        bucketName: String = "Camera"
    ): MediaItem {
        return MediaItem(
            id = id,
            displayName = displayName,
            mimeType = if (kind == MediaKind.IMAGE) "image/jpeg" else "video/mp4",
            kind = kind,
            sizeBytes = sizeBytes,
            dateAddedMillis = captureDateMillis ?: 1_700_000_000_000L,
            dateModifiedMillis = captureDateMillis ?: 1_700_000_000_000L,
            captureDateMillis = captureDateMillis,
            width = width,
            height = height,
            cameraMake = cameraMake,
            cameraModel = cameraModel,
            bucketId = bucketId,
            bucketName = bucketName
        )
    }

    @Test
    fun identicalContentGroupsTogetherEvenWhenNamesTimestampsAndBucketsDiffer() {
        val sharedBytes = "identical-photo-payload-bytes-9876543210".toByteArray(Charsets.UTF_8)
        val size = sharedBytes.size.toLong()

        val first = sampleItem(
            id = "content://media/external/images/media/10",
            displayName = "IMG_20260101_090000.jpg",
            sizeBytes = size,
            captureDateMillis = 1_700_000_000_000L,
            bucketId = "b_cam",
            bucketName = "Camera"
        )
        val second = sampleItem(
            id = "content://media/external/images/media/25",
            displayName = "WhatsApp Image 2026-01-05.jpg",
            sizeBytes = size,
            captureDateMillis = 1_700_500_000_000L,
            bucketId = "b_wa",
            bucketName = "WhatsApp Images"
        )

        val source = ContentHashSource.fromByteMap(
            mapOf(
                first.id to sharedBytes,
                second.id to sharedBytes.copyOf()
            )
        )

        val result = DuplicateDetector.findDuplicates(listOf(first, second), source)

        assertFalse(result.wasCancelled)
        assertEquals(1, result.totalGroupCount)
        assertEquals(1, result.totalDuplicateCount)
        val group = result.groups.single()
        assertEquals(Sha256Hasher.hashBytes(sharedBytes), group.sha256)
        assertEquals(2, group.allItems.size)
        assertEquals(setOf(first.id, second.id), group.allItems.map { it.id }.toSet())
    }

    @Test
    fun sameSizeDifferentContentDoesNotGroupTogether() {
        // Exactly the same byte length, completely different byte content, even with identical names/dates.
        val bytesA = "AAAA_same_length_payload_32bytes".toByteArray(Charsets.UTF_8)
        val bytesB = "BBBB_same_length_payload_32bytes".toByteArray(Charsets.UTF_8)
        assertEquals(bytesA.size, bytesB.size)

        val itemA = sampleItem(
            id = "id_a",
            displayName = "IMG_0001.jpg",
            sizeBytes = bytesA.size.toLong(),
            captureDateMillis = 1_700_000_000_000L
        )
        val itemB = sampleItem(
            id = "id_b",
            displayName = "IMG_0001.jpg",
            sizeBytes = bytesB.size.toLong(),
            captureDateMillis = 1_700_000_000_000L
        )

        val source = ContentHashSource.fromByteMap(
            mapOf(
                itemA.id to bytesA,
                itemB.id to bytesB
            )
        )

        val result = DuplicateDetector.findDuplicates(listOf(itemA, itemB), source)

        assertFalse(result.wasCancelled)
        assertEquals(2, result.hashedCandidateCount)
        assertEquals(0, result.totalGroupCount)
        assertTrue(result.groups.isEmpty())
        assertEquals(0L, result.totalRecoverableBytes)
    }

    @Test
    fun uniqueSizeFilesArePreFilteredWithoutHashingStreams() {
        val openedCount = AtomicInteger(0)
        val unique1 = sampleItem(id = "u1", sizeBytes = 101L)
        val unique2 = sampleItem(id = "u2", sizeBytes = 202L)
        val unique3 = sampleItem(id = "u3", sizeBytes = 303L)

        val source = ContentHashSource {
            openedCount.incrementAndGet()
            throw AssertionError("Unique-size files should be pre-filtered before opening streams")
        }

        val result = DuplicateDetector.findDuplicates(listOf(unique1, unique2, unique3), source)

        assertEquals(0, openedCount.get())
        assertEquals(3, result.skippedUniqueSizeCount)
        assertEquals(0, result.hashedCandidateCount)
        assertTrue(result.groups.isEmpty())
    }

    @Test
    fun keepRecommendationPrefersHigherResolutionAndExplainsWhy() {
        val lowRes = sampleItem(
            id = "item_low",
            displayName = "IMG_0001.jpg",
            width = 1280,
            height = 720,
            captureDateMillis = 1_600_000_000_000L,
            bucketName = "Camera"
        )
        val highRes = sampleItem(
            id = "item_high",
            displayName = "IMG_0001_copy.jpg",
            width = 4032,
            height = 3024,
            captureDateMillis = 1_700_000_000_000L,
            bucketName = "Downloads"
        )
        val unknownRes = sampleItem(
            id = "item_unknown",
            displayName = "IMG_0001_old.jpg",
            width = null,
            height = null,
            captureDateMillis = 1_500_000_000_000L,
            bucketName = "Camera"
        )

        val choice = KeepRecommender.chooseKeeper(listOf(lowRes, highRes, unknownRes))

        assertEquals(highRes.id, choice.keeper.id)
        assertEquals(listOf(lowRes.id, unknownRes.id), choice.duplicates.map { it.id })
        assertTrue(
            "Expected reason to mention highest resolution, got: ${choice.reason}",
            choice.reason.contains("highest resolution", ignoreCase = true)
        )
        assertTrue(choice.reason.contains("4032 × 3024"))
    }

    @Test
    fun keepRecommendationIsDeterministicAcrossAllInputPermutations() {
        val a = sampleItem(
            id = "id_1",
            displayName = "IMG_01.jpg",
            width = 3840,
            height = 2160,
            cameraMake = "Samsung",
            cameraModel = "SM-S928U",
            captureDateMillis = 1_700_000_100_000L,
            bucketName = "Camera"
        )
        val b = sampleItem(
            id = "id_2",
            displayName = "IMG_01 (1).jpg",
            width = 3840,
            height = 2160,
            cameraMake = "Samsung",
            cameraModel = "SM-S928U",
            captureDateMillis = 1_700_000_200_000L,
            bucketName = "Camera"
        )
        val c = sampleItem(
            id = "id_3",
            displayName = "IMG_01.jpg",
            width = 1920,
            height = 1080,
            cameraMake = "Samsung",
            cameraModel = "SM-S928U",
            captureDateMillis = 1_700_000_000_000L,
            bucketName = "Camera"
        )
        val d = sampleItem(
            id = "id_4",
            displayName = "IMG_01.jpg",
            width = 3840,
            height = 2160,
            cameraMake = null,
            cameraModel = null,
            captureDateMillis = 1_699_000_000_000L,
            bucketName = "Camera"
        )

        val permutations = listOf(
            listOf(a, b, c, d),
            listOf(d, c, b, a),
            listOf(b, d, a, c),
            listOf(c, a, d, b),
            listOf(d, a, c, b),
            listOf(b, c, d, a)
        )

        val baseline = KeepRecommender.chooseKeeper(permutations.first())
        assertEquals(a.id, baseline.keeper.id)

        for (perm in permutations) {
            val choice = KeepRecommender.chooseKeeper(perm)
            assertEquals(baseline.keeper.id, choice.keeper.id)
            assertEquals(baseline.duplicates.map { it.id }, choice.duplicates.map { it.id })
            assertEquals(baseline.reason, choice.reason)
        }
    }

    @Test
    fun recoverableBytesArithmeticAcrossMultipleGroups() {
        // Group 1: 3 identical copies of 5,000 bytes -> keeper = 1, duplicates = 2 -> 10,000 recoverable bytes
        // Group 2: 4 identical copies of 2,500 bytes -> keeper = 1, duplicates = 3 -> 7,500 recoverable bytes
        // Non-duplicate same-size pair of 2,500 bytes -> 0 recoverable bytes
        // Unique size file of 9,999 bytes -> 0 recoverable bytes
        // Total recoverable = 17,500 bytes
        val payload1 = ByteArray(5_000) { 0x11 }
        val payload2 = ByteArray(2_500) { 0x22 }
        val payloadOther2500 = ByteArray(2_500) { 0x33 }
        val payloadUnique = ByteArray(9_999) { 0x44 }

        val g1a = sampleItem(id = "g1_a", sizeBytes = 5_000L, width = 4000, height = 3000)
        val g1b = sampleItem(id = "g1_b", sizeBytes = 5_000L, width = 2000, height = 1500)
        val g1c = sampleItem(id = "g1_c", sizeBytes = 5_000L, width = 2000, height = 1500)

        val g2a = sampleItem(id = "g2_a", sizeBytes = 2_500L)
        val g2b = sampleItem(id = "g2_b", sizeBytes = 2_500L)
        val g2c = sampleItem(id = "g2_c", sizeBytes = 2_500L)
        val g2d = sampleItem(id = "g2_d", sizeBytes = 2_500L)

        val sameSizeDiffContent = sampleItem(id = "diff_2500", sizeBytes = 2_500L)
        val uniqueItem = sampleItem(id = "unique_9999", sizeBytes = 9_999L)

        val items = listOf(g1a, g1b, g1c, g2a, g2b, g2c, g2d, sameSizeDiffContent, uniqueItem)
        val source = ContentHashSource.fromByteMap(
            mapOf(
                g1a.id to payload1,
                g1b.id to payload1,
                g1c.id to payload1,
                g2a.id to payload2,
                g2b.id to payload2,
                g2c.id to payload2,
                g2d.id to payload2,
                sameSizeDiffContent.id to payloadOther2500,
                uniqueItem.id to payloadUnique
            )
        )

        val scanResult = DuplicateDetector.findDuplicates(items, source)

        assertEquals(2, scanResult.totalGroupCount)
        assertEquals(5, scanResult.totalDuplicateCount)
        assertEquals(10_000L, scanResult.groups[0].recoverableBytes)
        assertEquals(15_000L, scanResult.groups[0].totalBytes)
        assertEquals(7_500L, scanResult.groups[1].recoverableBytes)
        assertEquals(10_000L, scanResult.groups[1].totalBytes)
        assertEquals(17_500L, scanResult.totalRecoverableBytes)

        val report = DuplicateDetector.buildReadOnlyReport(scanResult)
        assertEquals(17_500L, report.totalRecoverableBytes)
        assertEquals(5, report.duplicateFileCount)
        assertTrue(report.formattedText.contains("READ-ONLY"))
    }

    @Test
    fun cancellationDuringHashingDiscardsPartialResults() {
        val bytes = "duplicate-payload".toByteArray(Charsets.UTF_8)
        val item1 = sampleItem(id = "c1", sizeBytes = bytes.size.toLong())
        val item2 = sampleItem(id = "c2", sizeBytes = bytes.size.toLong())

        var reads = 0
        val source = ContentHashSource {
            reads++
            object : InputStream() {
                override fun read(): Int = -1
            }
        }

        val scanResult = DuplicateDetector.findDuplicates(
            items = listOf(item1, item2),
            source = source,
            isCancelled = { reads >= 1 }
        )

        assertTrue(scanResult.wasCancelled)
        assertTrue(scanResult.groups.isEmpty())
        assertEquals(0L, scanResult.totalRecoverableBytes)
    }

    @Test
    fun unreadableCandidateIsRecordedInFailuresWithoutAbortingOtherGroups() {
        val bytes = "valid-duplicate-content".toByteArray(Charsets.UTF_8)
        val valid1 = sampleItem(id = "v1", sizeBytes = bytes.size.toLong())
        val valid2 = sampleItem(id = "v2", sizeBytes = bytes.size.toLong())
        val broken = sampleItem(id = "broken", sizeBytes = bytes.size.toLong())

        val source = ContentHashSource { item ->
            if (item.id == "broken") {
                throw IOException("Simulated I/O read error")
            }
            bytes.inputStream()
        }

        val result = DuplicateDetector.findDuplicates(listOf(valid1, valid2, broken), source)

        assertFalse(result.wasCancelled)
        assertEquals(1, result.failures.size)
        assertEquals("broken", result.failures.single().item.id)
        assertEquals(1, result.totalGroupCount)
        assertEquals(bytes.size.toLong(), result.totalRecoverableBytes)
    }
}
