package com.bhavb13.dtm.organizer.core

/**
 * Pure Kotlin permission evaluation and library degradation model across Android API levels:
 * - Below API 33: `READ_EXTERNAL_STORAGE`
 * - API 33: `READ_MEDIA_IMAGES` and `READ_MEDIA_VIDEO`
 * - API 34+: `READ_MEDIA_IMAGES`, `READ_MEDIA_VIDEO`, and `READ_MEDIA_VISUAL_USER_SELECTED`
 *   (including Android 14+ partial photo/video access).
 *
 * Ensures partial or denied permissions always produce an explicit, explained degradation state
 * rather than silently presenting an empty or partial result as if the device had no other media.
 */
object MediaPermissions {
    const val READ_EXTERNAL_STORAGE = "android.permission.READ_EXTERNAL_STORAGE"
    const val READ_MEDIA_IMAGES = "android.permission.READ_MEDIA_IMAGES"
    const val READ_MEDIA_VIDEO = "android.permission.READ_MEDIA_VIDEO"
    const val READ_MEDIA_VISUAL_USER_SELECTED = "android.permission.READ_MEDIA_VISUAL_USER_SELECTED"

    fun requiredPermissionsForSdk(sdkInt: Int): List<String> {
        return when {
            sdkInt >= 34 -> listOf(
                READ_MEDIA_IMAGES,
                READ_MEDIA_VIDEO,
                READ_MEDIA_VISUAL_USER_SELECTED
            )
            sdkInt == 33 -> listOf(
                READ_MEDIA_IMAGES,
                READ_MEDIA_VIDEO
            )
            else -> listOf(READ_EXTERNAL_STORAGE)
        }
    }
}

enum class AccessLevel {
    /** All photos and videos in MediaStore are accessible. */
    FULL,
    /**
     * Only a subset of media is visible (e.g. Android 14+ user-selected items, or only one of
     * images/videos granted on Android 13+).
     */
    PARTIAL,
    /** No MediaStore permissions are granted. */
    DENIED
}

data class PermissionStatus(
    val sdkInt: Int,
    val accessLevel: AccessLevel,
    val canReadImages: Boolean,
    val canReadVideos: Boolean,
    val isUserSelectedOnly: Boolean,
    val headline: String,
    val explanation: String,
    val unseenScopeWarnings: List<String>
) {
    val isFullAccess: Boolean
        get() = accessLevel == AccessLevel.FULL

    val isDegraded: Boolean
        get() = accessLevel != AccessLevel.FULL
}

/**
 * Represents the evaluated state of a library load combined with the active [PermissionStatus].
 * Prevents callers and UI from treating a partial-permission or denied-permission load as a normal
 * full library result (especially when 0 items are returned).
 */
sealed class LibraryAccessOutcome {
    abstract val items: List<MediaItem>
    abstract val permissionStatus: PermissionStatus
    abstract val isDegraded: Boolean
    abstract val explanation: String

    /** Full access granted; [items] represents the complete MediaStore library. */
    data class FullSuccess(
        override val items: List<MediaItem>,
        override val permissionStatus: PermissionStatus
    ) : LibraryAccessOutcome() {
        override val isDegraded: Boolean = false
        override val explanation: String = permissionStatus.explanation
    }

    /**
     * Partial access granted (such as `READ_MEDIA_VISUAL_USER_SELECTED` on API 34+ or single-kind
     * grant on API 33+). Even if [items] is empty or non-empty, this is an explained degradation
     * rather than an unannotated full success.
     */
    data class ExplainedDegradation(
        override val items: List<MediaItem>,
        override val permissionStatus: PermissionStatus,
        override val explanation: String,
        val unseenScopes: List<String>
    ) : LibraryAccessOutcome() {
        override val isDegraded: Boolean = true
    }

    /**
     * MediaStore permissions are denied. Never reported as an empty library success.
     */
    data class AccessDenied(
        override val items: List<MediaItem>,
        override val permissionStatus: PermissionStatus,
        override val explanation: String
    ) : LibraryAccessOutcome() {
        override val isDegraded: Boolean = true
    }
}

object PermissionEvaluator {

    /**
     * Evaluates runtime permission grants for the given [sdkInt].
     *
     * @param sdkInt Android `Build.VERSION.SDK_INT`
     * @param grantedPermissions set of currently granted permission strings
     */
    fun evaluate(sdkInt: Int, grantedPermissions: Set<String>): PermissionStatus {
        if (sdkInt < 33) {
            val hasExternal = MediaPermissions.READ_EXTERNAL_STORAGE in grantedPermissions
            return if (hasExternal) {
                PermissionStatus(
                    sdkInt = sdkInt,
                    accessLevel = AccessLevel.FULL,
                    canReadImages = true,
                    canReadVideos = true,
                    isUserSelectedOnly = false,
                    headline = "Full media library access",
                    explanation = "All photos and videos indexed by MediaStore are visible.",
                    unseenScopeWarnings = emptyList()
                )
            } else {
                PermissionStatus(
                    sdkInt = sdkInt,
                    accessLevel = AccessLevel.DENIED,
                    canReadImages = false,
                    canReadVideos = false,
                    isUserSelectedOnly = false,
                    headline = "Storage permission denied",
                    explanation = "READ_EXTERNAL_STORAGE is not granted. The app cannot see your photos or videos in MediaStore until storage read permission is granted or a folder is opened via SAF.",
                    unseenScopeWarnings = listOf(
                        "All device photos and videos in MediaStore (READ_EXTERNAL_STORAGE denied)"
                    )
                )
            }
        }

        val hasFullImages = MediaPermissions.READ_MEDIA_IMAGES in grantedPermissions
        val hasFullVideos = MediaPermissions.READ_MEDIA_VIDEO in grantedPermissions
        val hasVisualSelected = sdkInt >= 34 &&
            MediaPermissions.READ_MEDIA_VISUAL_USER_SELECTED in grantedPermissions

        // Full access: both READ_MEDIA_IMAGES and READ_MEDIA_VIDEO are granted
        if (hasFullImages && hasFullVideos) {
            return PermissionStatus(
                sdkInt = sdkInt,
                accessLevel = AccessLevel.FULL,
                canReadImages = true,
                canReadVideos = true,
                isUserSelectedOnly = false,
                headline = "Full photo and video access",
                explanation = "All photos and videos in MediaStore are visible for browsing and SHA-256 duplicate analysis.",
                unseenScopeWarnings = emptyList()
            )
        }

        // Partial access cases on API 33+ / 34+
        if (hasFullImages || hasFullVideos || hasVisualSelected) {
            val warnings = mutableListOf<String>()
            val isUserSelectedOnly = hasVisualSelected && !hasFullImages && !hasFullVideos

            if (isUserSelectedOnly) {
                warnings.add(
                    "Unselected photos and videos on the device are hidden because Android 14+ 'Select photos and videos' partial access is active."
                )
            } else {
                if (!hasFullImages) {
                    warnings.add("Photos are hidden because READ_MEDIA_IMAGES is not granted.")
                }
                if (!hasFullVideos) {
                    warnings.add("Videos are hidden because READ_MEDIA_VIDEO is not granted.")
                }
                if (hasVisualSelected) {
                    warnings.add("Only user-selected items are visible for the ungranted media type.")
                }
            }

            val explanation = buildString {
                append("Partial media access is active: ")
                append(warnings.joinToString(" "))
                append(" Duplicate detection and storage summaries reflect only the items Android allows this app to see.")
            }

            return PermissionStatus(
                sdkInt = sdkInt,
                accessLevel = AccessLevel.PARTIAL,
                canReadImages = hasFullImages || hasVisualSelected,
                canReadVideos = hasFullVideos || hasVisualSelected,
                isUserSelectedOnly = isUserSelectedOnly,
                headline = if (isUserSelectedOnly) {
                    "Partial access: selected photos & videos only"
                } else {
                    "Partial access: limited media types"
                },
                explanation = explanation,
                unseenScopeWarnings = warnings
            )
        }

        // Denied on API 33+
        return PermissionStatus(
            sdkInt = sdkInt,
            accessLevel = AccessLevel.DENIED,
            canReadImages = false,
            canReadVideos = false,
            isUserSelectedOnly = false,
            headline = "Media permissions not granted",
            explanation = "Photo and video permissions (READ_MEDIA_IMAGES / READ_MEDIA_VIDEO) are not granted. The app cannot see your device media library—an empty list here does not mean your phone has no media.",
            unseenScopeWarnings = listOf(
                "All device photos (READ_MEDIA_IMAGES not granted)",
                "All device videos (READ_MEDIA_VIDEO not granted)"
            )
        )
    }

    /**
     * Combines a loaded item list with the current [PermissionStatus] so partial or denied access
     * always produces an explicit [LibraryAccessOutcome.ExplainedDegradation] or
     * [LibraryAccessOutcome.AccessDenied], never a silent empty full-access result.
     */
    fun resolveLibraryOutcome(
        items: List<MediaItem>,
        permissionStatus: PermissionStatus
    ): LibraryAccessOutcome {
        return when (permissionStatus.accessLevel) {
            AccessLevel.FULL -> LibraryAccessOutcome.FullSuccess(
                items = items,
                permissionStatus = permissionStatus
            )
            AccessLevel.PARTIAL -> {
                val itemDetail = if (items.isEmpty()) {
                    "No visible items were returned under your current partial permission selection (the rest of your device library remains hidden). "
                } else {
                    "Showing ${items.size} visible item(s); other media on your device is hidden by partial permission settings. "
                }
                LibraryAccessOutcome.ExplainedDegradation(
                    items = items,
                    permissionStatus = permissionStatus,
                    explanation = itemDetail + permissionStatus.explanation,
                    unseenScopes = permissionStatus.unseenScopeWarnings
                )
            }
            AccessLevel.DENIED -> LibraryAccessOutcome.AccessDenied(
                items = items,
                permissionStatus = permissionStatus,
                explanation = permissionStatus.explanation
            )
        }
    }
}
