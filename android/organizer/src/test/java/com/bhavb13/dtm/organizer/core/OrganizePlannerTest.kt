package com.bhavb13.dtm.organizer.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Calendar
import java.util.TimeZone

class OrganizePlannerTest {

    private val testTimeZone = TimeZone.getTimeZone("UTC")

    private fun sampleItem(
        id: String,
        displayName: String = "photo_$id.jpg",
        captureDateMillis: Long? = 1_700_000_000_000L, // 2023-11-14T22:13:20Z
        dateModifiedMillis: Long = 1_700_000_000_000L,
        sizeBytes: Long = 10_000L
    ): MediaItem {
        return MediaItem(
            id = id,
            displayName = displayName,
            mimeType = "image/jpeg",
            kind = MediaKind.IMAGE,
            sizeBytes = sizeBytes,
            dateAddedMillis = dateModifiedMillis,
            dateModifiedMillis = dateModifiedMillis,
            captureDateMillis = captureDateMillis,
            bucketId = "bucket_cam",
            bucketName = "Camera"
        )
    }

    @Test
    fun templateYearMonthRendersCorrectlyFromDateTaken() {
        val cal = Calendar.getInstance(testTimeZone).apply {
            set(2025, Calendar.MARCH, 25, 10, 0, 0)
        }
        val item = sampleItem(
            id = "1",
            captureDateMillis = cal.timeInMillis,
            dateModifiedMillis = 0L
        )

        val folder = OrganizeTemplate.YEAR_MONTH.render(
            captureDateMillis = item.captureDateMillis,
            dateModifiedMillis = item.dateModifiedMillis,
            timeZone = testTimeZone
        )
        assertEquals("2025/03", folder)
    }

    @Test
    fun templateYearMonthFallsBackToDateModifiedWhenDateTakenIsMissing() {
        val cal = Calendar.getInstance(testTimeZone).apply {
            set(2024, Calendar.OCTOBER, 12, 14, 0, 0)
        }
        val item = sampleItem(
            id = "2",
            captureDateMillis = null,
            dateModifiedMillis = cal.timeInMillis
        )

        val folder = OrganizeTemplate.YEAR_MONTH.render(
            captureDateMillis = item.captureDateMillis,
            dateModifiedMillis = item.dateModifiedMillis,
            timeZone = testTimeZone
        )
        assertEquals("2024/10", folder)
    }

    @Test
    fun templateYearMonthDayRendersFullDate() {
        val cal = Calendar.getInstance(testTimeZone).apply {
            set(2026, Calendar.JANUARY, 5, 8, 30, 0)
        }
        val item = sampleItem(
            id = "3",
            captureDateMillis = cal.timeInMillis
        )

        val folder = OrganizeTemplate.YEAR_MONTH_DAY.render(
            captureDateMillis = item.captureDateMillis,
            dateModifiedMillis = item.dateModifiedMillis,
            timeZone = testTimeZone
        )
        assertEquals("2026/01/05", folder)
    }

    @Test
    fun conflictNamingProducesNumberedCopies() {
        assertEquals("photo (1).jpg", ConflictNaming.generateCandidateName("photo.jpg", 1))
        assertEquals("photo (2).jpg", ConflictNaming.generateCandidateName("photo.jpg", 2))
        assertEquals("document (1)", ConflictNaming.generateCandidateName("document", 1))
        assertEquals("my.photo (1).png", ConflictNaming.generateCandidateName("my.photo.png", 1))
    }

    @Test
    fun planningWithCleanDestinationPlansAllAsCopy() {
        val cal1 = Calendar.getInstance(testTimeZone).apply { set(2025, Calendar.MAY, 1) }
        val cal2 = Calendar.getInstance(testTimeZone).apply { set(2025, Calendar.JUNE, 1) }

        val item1 = sampleItem("1", "A.jpg", captureDateMillis = cal1.timeInMillis)
        val item2 = sampleItem("2", "B.jpg", captureDateMillis = cal2.timeInMillis)

        val plan = OrganizePlanner.plan(
            items = listOf(item1, item2),
            template = OrganizeTemplate.YEAR_MONTH,
            destinationTreeUri = "content://com.android.externalstorage.documents/tree/primary%3ATest",
            destinationLabel = "Test Folder",
            timeZone = testTimeZone
        )

        assertEquals(2, plan.totalSourceCount)
        assertEquals(2, plan.toCopyCount)
        assertEquals(0, plan.toSkipCount)
        assertEquals(0, plan.conflictCount)
        assertEquals(20_000L, plan.totalCopyBytes)
        assertEquals(listOf("2025/05", "2025/06"), plan.destinationFolders)

        assertEquals(PlannedAction.COPY, plan.items[0].action)
        assertEquals("A.jpg", plan.items[0].targetFileName)
        assertEquals("2025/05", plan.items[0].destinationSubfolder)

        assertEquals(PlannedAction.COPY, plan.items[1].action)
        assertEquals("B.jpg", plan.items[1].targetFileName)
        assertEquals("2025/06", plan.items[1].destinationSubfolder)
    }

    @Test
    fun planningSkipsWhenDestinationHasSameNameAndIdenticalSha256() {
        val cal = Calendar.getInstance(testTimeZone).apply { set(2025, Calendar.MAY, 1) }
        val item = sampleItem("1", "duplicate.jpg", captureDateMillis = cal.timeInMillis)

        val sha = "abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234abcd1234"
        val existingDestFiles = mapOf(
            "2025/05/duplicate.jpg" to ExistingDestinationFile(
                fileName = "duplicate.jpg",
                subfolder = "2025/05",
                sizeBytes = 10_000L,
                sha256 = sha
            )
        )

        val plan = OrganizePlanner.plan(
            items = listOf(item),
            template = OrganizeTemplate.YEAR_MONTH,
            destinationTreeUri = "content://tree/dest",
            destinationLabel = "Dest",
            destinationFiles = { subfolder, name -> existingDestFiles["$subfolder/$name"] },
            sourceSha256Provider = { sha },
            timeZone = testTimeZone
        )

        assertEquals(1, plan.totalSourceCount)
        assertEquals(0, plan.toCopyCount)
        assertEquals(1, plan.toSkipCount)
        assertEquals(0, plan.conflictCount)
        assertEquals(0L, plan.totalCopyBytes)
        assertEquals(PlannedAction.SKIP_IDENTICAL, plan.items[0].action)
        assertEquals("duplicate.jpg", plan.items[0].targetFileName)
    }

    @Test
    fun planningRenamesWhenDestinationHasSameNameWithDifferentContent() {
        val cal = Calendar.getInstance(testTimeZone).apply { set(2025, Calendar.MAY, 1) }
        val item = sampleItem("1", "photo.jpg", captureDateMillis = cal.timeInMillis)

        val sourceSha = "1111111111111111111111111111111111111111111111111111111111111111"
        val destSha = "2222222222222222222222222222222222222222222222222222222222222222"

        val existingDestFiles = mapOf(
            "2025/05/photo.jpg" to ExistingDestinationFile(
                fileName = "photo.jpg",
                subfolder = "2025/05",
                sizeBytes = 10_000L,
                sha256 = destSha
            )
        )

        val plan = OrganizePlanner.plan(
            items = listOf(item),
            template = OrganizeTemplate.YEAR_MONTH,
            destinationTreeUri = "content://tree/dest",
            destinationLabel = "Dest",
            destinationFiles = { subfolder, name -> existingDestFiles["$subfolder/$name"] },
            sourceSha256Provider = { sourceSha },
            timeZone = testTimeZone
        )

        assertEquals(1, plan.totalSourceCount)
        assertEquals(1, plan.toCopyCount)
        assertEquals(0, plan.toSkipCount)
        assertEquals(1, plan.conflictCount)
        assertEquals(PlannedAction.COPY_WITH_RENAME, plan.items[0].action)
        assertEquals("photo (1).jpg", plan.items[0].targetFileName)
    }

    @Test
    fun planningResolvesNameCollisionsWithinSameBatch() {
        val cal = Calendar.getInstance(testTimeZone).apply { set(2025, Calendar.MAY, 1) }
        val itemA = sampleItem("1", "vacation.jpg", captureDateMillis = cal.timeInMillis)
        val itemB = sampleItem("2", "vacation.jpg", captureDateMillis = cal.timeInMillis)

        val shaA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        val shaB = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

        val hashes = mapOf(itemA.id to shaA, itemB.id to shaB)

        val plan = OrganizePlanner.plan(
            items = listOf(itemA, itemB),
            template = OrganizeTemplate.YEAR_MONTH,
            destinationTreeUri = "content://tree/dest",
            destinationLabel = "Dest",
            sourceSha256Provider = { hashes[it.id] },
            timeZone = testTimeZone
        )

        assertEquals(2, plan.totalSourceCount)
        assertEquals(2, plan.toCopyCount)
        assertEquals(1, plan.conflictCount)

        assertEquals("vacation.jpg", plan.items[0].targetFileName)
        assertEquals(PlannedAction.COPY, plan.items[0].action)

        assertEquals("vacation (1).jpg", plan.items[1].targetFileName)
        assertEquals(PlannedAction.COPY_WITH_RENAME, plan.items[1].action)
    }

    @Test
    fun planningSkipsDuplicateWithinSameBatchIfContentIsIdentical() {
        val cal = Calendar.getInstance(testTimeZone).apply { set(2025, Calendar.MAY, 1) }
        val itemA = sampleItem("1", "identical.jpg", captureDateMillis = cal.timeInMillis)
        val itemB = sampleItem("2", "identical.jpg", captureDateMillis = cal.timeInMillis)

        val sharedSha = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

        val plan = OrganizePlanner.plan(
            items = listOf(itemA, itemB),
            template = OrganizeTemplate.YEAR_MONTH,
            destinationTreeUri = "content://tree/dest",
            destinationLabel = "Dest",
            sourceSha256Provider = { sharedSha },
            timeZone = testTimeZone
        )

        assertEquals(2, plan.totalSourceCount)
        assertEquals(1, plan.toCopyCount)
        assertEquals(1, plan.toSkipCount)

        assertEquals("identical.jpg", plan.items[0].targetFileName)
        assertEquals(PlannedAction.COPY, plan.items[0].action)

        assertEquals(PlannedAction.SKIP_IDENTICAL, plan.items[1].action)
    }

    @Test
    fun sameNameInOneBatchIsRenamedEvenWhenHashesAreUnknown() {
        val cal = Calendar.getInstance(testTimeZone).apply { set(2025, Calendar.MAY, 1) }
        val itemA = sampleItem("1", "IMG_0001.jpg", captureDateMillis = cal.timeInMillis)
        val itemB = sampleItem("2", "IMG_0001.jpg", captureDateMillis = cal.timeInMillis)

        val plan = OrganizePlanner.plan(
            items = listOf(itemA, itemB),
            template = OrganizeTemplate.YEAR_MONTH,
            destinationTreeUri = "content://tree/dest",
            destinationLabel = "Dest",
            sourceSha256Provider = { null },
            timeZone = testTimeZone
        )

        assertEquals(listOf("IMG_0001.jpg", "IMG_0001 (1).jpg"), plan.items.map { it.targetFileName })
        assertEquals(2, plan.toCopyCount)
    }

    @Test
    fun sourcesAreHashedOnlyWhenTheirNameCollides() {
        val cal = Calendar.getInstance(testTimeZone).apply { set(2025, Calendar.MAY, 1) }
        val items = (1..5).map { sampleItem(it.toString(), "unique_$it.jpg", captureDateMillis = cal.timeInMillis) }
        val hashed = mutableListOf<String>()

        OrganizePlanner.plan(
            items = items,
            template = OrganizeTemplate.YEAR_MONTH,
            destinationTreeUri = "content://tree/dest",
            destinationLabel = "Dest",
            sourceSha256Provider = { hashed += it.id; "x" },
            timeZone = testTimeZone
        )

        assertEquals(emptyList<String>(), hashed)
    }

    @Test
    fun destinationFileWithUnknownHashIsNeverTreatedAsIdentical() {
        val cal = Calendar.getInstance(testTimeZone).apply { set(2025, Calendar.MAY, 1) }
        val item = sampleItem("1", "photo.jpg", captureDateMillis = cal.timeInMillis)
        val existing = ExistingDestinationFile("photo.jpg", "2025/05", 10L, sha256 = null, documentUri = "content://doc/1")

        val plan = OrganizePlanner.plan(
            items = listOf(item),
            template = OrganizeTemplate.YEAR_MONTH,
            destinationTreeUri = "content://tree/dest",
            destinationLabel = "Dest",
            destinationFiles = { _, name -> if (name == "photo.jpg") existing else null },
            sourceSha256Provider = { "same" },
            destSha256Provider = { null },
            timeZone = testTimeZone
        )

        assertEquals(PlannedAction.COPY_WITH_RENAME, plan.items[0].action)
        assertEquals("photo (1).jpg", plan.items[0].targetFileName)
    }
}
