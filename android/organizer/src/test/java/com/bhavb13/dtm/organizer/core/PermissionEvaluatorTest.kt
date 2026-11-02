package com.bhavb13.dtm.organizer.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class PermissionEvaluatorTest {

    private val dummyItem = MediaItem(
        id = "1",
        displayName = "IMG_001.jpg",
        mimeType = "image/jpeg",
        kind = MediaKind.IMAGE,
        sizeBytes = 1024L,
        dateAddedMillis = 1000L,
        dateModifiedMillis = 1000L
    )

    @Test
    fun belowApi33UsesReadExternalStorage() {
        val grantedStatus = PermissionEvaluator.evaluate(
            sdkInt = 30,
            grantedPermissions = setOf(MediaPermissions.READ_EXTERNAL_STORAGE)
        )
        assertEquals(AccessLevel.FULL, grantedStatus.accessLevel)
        assertTrue(grantedStatus.canReadImages)
        assertTrue(grantedStatus.canReadVideos)
        assertFalse(grantedStatus.isDegraded)

        val deniedStatus = PermissionEvaluator.evaluate(
            sdkInt = 30,
            grantedPermissions = emptySet()
        )
        assertEquals(AccessLevel.DENIED, deniedStatus.accessLevel)
        assertFalse(deniedStatus.canReadImages)
        assertFalse(deniedStatus.canReadVideos)
        assertTrue(deniedStatus.isDegraded)
        assertTrue(deniedStatus.explanation.contains("READ_EXTERNAL_STORAGE"))
    }

    @Test
    fun api33RequiresBothImageAndVideoPermissionsForFullAccess() {
        val fullStatus = PermissionEvaluator.evaluate(
            sdkInt = 33,
            grantedPermissions = setOf(
                MediaPermissions.READ_MEDIA_IMAGES,
                MediaPermissions.READ_MEDIA_VIDEO
            )
        )
        assertEquals(AccessLevel.FULL, fullStatus.accessLevel)
        assertFalse(fullStatus.isDegraded)

        // Only images granted -> PARTIAL with explanation that videos cannot be seen
        val imagesOnly = PermissionEvaluator.evaluate(
            sdkInt = 33,
            grantedPermissions = setOf(MediaPermissions.READ_MEDIA_IMAGES)
        )
        assertEquals(AccessLevel.PARTIAL, imagesOnly.accessLevel)
        assertTrue(imagesOnly.canReadImages)
        assertFalse(imagesOnly.canReadVideos)
        assertTrue(imagesOnly.isDegraded)
        assertTrue(imagesOnly.explanation.contains("READ_MEDIA_VIDEO"))
    }

    @Test
    fun api34PartialUserSelectedPermissionProducesExplainedDegradationRatherThanEmptySuccess() {
        val partialStatus = PermissionEvaluator.evaluate(
            sdkInt = 34,
            grantedPermissions = setOf(MediaPermissions.READ_MEDIA_VISUAL_USER_SELECTED)
        )

        assertEquals(AccessLevel.PARTIAL, partialStatus.accessLevel)
        assertTrue(partialStatus.isUserSelectedOnly)
        assertTrue(partialStatus.isDegraded)
        assertTrue(partialStatus.canReadImages)
        assertTrue(partialStatus.canReadVideos)

        // Crucial check: even when 0 items are returned under partial access, the outcome is
        // ExplainedDegradation (never FullSuccess) and explicitly explains what cannot be seen.
        val emptyOutcome = PermissionEvaluator.resolveLibraryOutcome(
            items = emptyList(),
            permissionStatus = partialStatus
        )
        assertTrue(
            "Expected ExplainedDegradation for partial access, got: $emptyOutcome",
            emptyOutcome is LibraryAccessOutcome.ExplainedDegradation
        )
        val degraded = emptyOutcome as LibraryAccessOutcome.ExplainedDegradation
        assertTrue(degraded.isDegraded)
        assertTrue(degraded.explanation.contains("hidden", ignoreCase = true))
        assertTrue(degraded.explanation.contains("partial", ignoreCase = true))
        assertFalse(degraded.unseenScopes.isEmpty())

        // Also when some items are returned under partial access, it remains ExplainedDegradation.
        val nonEmptyOutcome = PermissionEvaluator.resolveLibraryOutcome(
            items = listOf(dummyItem),
            permissionStatus = partialStatus
        )
        assertTrue(nonEmptyOutcome is LibraryAccessOutcome.ExplainedDegradation)
        assertTrue(nonEmptyOutcome.isDegraded)
        assertEquals(1, nonEmptyOutcome.items.size)
    }

    @Test
    fun deniedPermissionProducesAccessDeniedOutcomeNeverEmptySuccess() {
        val deniedStatus = PermissionEvaluator.evaluate(
            sdkInt = 35,
            grantedPermissions = emptySet()
        )
        assertEquals(AccessLevel.DENIED, deniedStatus.accessLevel)

        val outcome = PermissionEvaluator.resolveLibraryOutcome(
            items = emptyList(),
            permissionStatus = deniedStatus
        )
        assertTrue(outcome is LibraryAccessOutcome.AccessDenied)
        assertTrue(outcome.isDegraded)
        assertTrue(outcome.explanation.contains("READ_MEDIA_IMAGES"))
    }

    @Test
    fun requiredPermissionsForSdkMatchesVersionContract() {
        assertEquals(
            listOf(MediaPermissions.READ_EXTERNAL_STORAGE),
            MediaPermissions.requiredPermissionsForSdk(26)
        )
        assertEquals(
            listOf(MediaPermissions.READ_EXTERNAL_STORAGE),
            MediaPermissions.requiredPermissionsForSdk(32)
        )
        assertEquals(
            listOf(MediaPermissions.READ_MEDIA_IMAGES, MediaPermissions.READ_MEDIA_VIDEO),
            MediaPermissions.requiredPermissionsForSdk(33)
        )
        assertEquals(
            listOf(
                MediaPermissions.READ_MEDIA_IMAGES,
                MediaPermissions.READ_MEDIA_VIDEO,
                MediaPermissions.READ_MEDIA_VISUAL_USER_SELECTED
            ),
            MediaPermissions.requiredPermissionsForSdk(34)
        )
        assertEquals(
            listOf(
                MediaPermissions.READ_MEDIA_IMAGES,
                MediaPermissions.READ_MEDIA_VIDEO,
                MediaPermissions.READ_MEDIA_VISUAL_USER_SELECTED
            ),
            MediaPermissions.requiredPermissionsForSdk(35)
        )
    }
}
