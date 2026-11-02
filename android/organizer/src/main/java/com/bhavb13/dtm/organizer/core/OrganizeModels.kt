package com.bhavb13.dtm.organizer.core

import java.util.Calendar
import java.util.Locale
import java.util.TimeZone

/**
 * Date-based subfolder templates for safe media organization.
 */
enum class OrganizeTemplate(val pattern: String, val displayName: String) {
    YEAR_MONTH("{year}/{month}", "Year/Month ({year}/{month})"),
    YEAR_MONTH_DAY("{year}/{month}/{day}", "Year/Month/Day ({year}/{month}/{day})");

    /**
     * Renders target relative subfolder (e.g. "2026/11" or "2026/11/02") from capture date
     * (MediaStore DATE_TAKEN, falling back to DATE_MODIFIED).
     */
    fun render(
        captureDateMillis: Long?,
        dateModifiedMillis: Long,
        timeZone: TimeZone = TimeZone.getDefault()
    ): String {
        val effectiveMillis = when {
            captureDateMillis != null && captureDateMillis > 0L -> captureDateMillis
            dateModifiedMillis > 0L -> dateModifiedMillis
            else -> 0L
        }
        if (effectiveMillis <= 0L) {
            return "Unknown_Date"
        }
        val cal = Calendar.getInstance(timeZone)
        cal.timeInMillis = effectiveMillis
        val year = String.format(Locale.US, "%04d", cal.get(Calendar.YEAR))
        val month = String.format(Locale.US, "%02d", cal.get(Calendar.MONTH) + 1)
        return when (this) {
            YEAR_MONTH -> "$year/$month"
            YEAR_MONTH_DAY -> {
                val day = String.format(Locale.US, "%02d", cal.get(Calendar.DAY_OF_MONTH))
                "$year/$month/$day"
            }
        }
    }
}

/**
 * Metadata for a file that already exists at the destination.
 */
data class ExistingDestinationFile(
    val fileName: String,
    val subfolder: String,
    val sizeBytes: Long,
    val sha256: String? = null,
    /** The provider's document URI, so its content can be hashed without guessing document ids. */
    val documentUri: String? = null
)

/**
 * Helper for generating non-clobbering candidate names like "name (1).ext".
 */
object ConflictNaming {
    fun generateCandidateName(originalName: String, copyIndex: Int): String {
        require(copyIndex >= 1) { "copyIndex must be >= 1" }
        val dot = originalName.lastIndexOf('.')
        return if (dot > 0) {
            val base = originalName.substring(0, dot)
            val ext = originalName.substring(dot)
            "$base ($copyIndex)$ext"
        } else {
            "$originalName ($copyIndex)"
        }
    }
}

/**
 * Action planned for a single media item.
 */
enum class PlannedAction {
    /** Target name is free; execute safe copy. */
    COPY,
    /** Existing destination file has identical name and SHA-256; skip. */
    SKIP_IDENTICAL,
    /** Existing destination file has same name but different content; copy as "name (1).ext". */
    COPY_WITH_RENAME
}

/**
 * Details of a planned item copy.
 */
data class PlannedCopyItem(
    val sourceItem: MediaItem,
    val destinationSubfolder: String,
    val targetFileName: String,
    val action: PlannedAction,
    val sourceSha256: String? = null,
    val existingDestinationSha256: String? = null,
    val reason: String
)

/**
 * Full pre-execution organize plan.
 * Pure Kotlin representation shown to the user before anything is written.
 */
data class OrganizePlan(
    val template: OrganizeTemplate,
    val destinationTreeUri: String,
    val destinationLabel: String,
    val items: List<PlannedCopyItem>,
    val totalSourceCount: Int,
    val toCopyCount: Int,
    val toSkipCount: Int,
    val conflictCount: Int,
    val totalCopyBytes: Long,
    val destinationFolders: List<String>
) {
    val hasWork: Boolean
        get() = toCopyCount > 0
}

/**
 * Progress snapshot emitted during safe copy execution.
 */
data class OrganizeProgress(
    val completedCount: Int,
    val totalCount: Int,
    val currentFileName: String,
    val bytesCopied: Long,
    val totalBytesToCopy: Long,
    val phase: String
) {
    val fraction: Float
        get() = if (totalCount <= 0) 0f else (completedCount.toFloat() / totalCount.toFloat()).coerceIn(0f, 1f)
}

/**
 * Record of a failed file copy during safe organize execution.
 */
data class OrganizeItemFailure(
    val item: MediaItem,
    val targetFileName: String,
    val targetSubfolder: String,
    val errorMessage: String
)

/**
 * Result of executing an organize plan.
 */
data class OrganizeResult(
    val totalPlanned: Int,
    val copiedCount: Int,
    val skippedCount: Int,
    val failureCount: Int,
    val failures: List<OrganizeItemFailure>,
    val wasCancelled: Boolean
) {
    val isSuccess: Boolean
        get() = !wasCancelled && failures.isEmpty()

    companion object {
        fun cancelled(
            totalPlanned: Int,
            copiedCount: Int,
            skippedCount: Int,
            failures: List<OrganizeItemFailure> = emptyList()
        ): OrganizeResult {
            return OrganizeResult(
                totalPlanned = totalPlanned,
                copiedCount = copiedCount,
                skippedCount = skippedCount,
                failureCount = failures.size,
                failures = failures,
                wasCancelled = true
            )
        }
    }
}

/**
 * Pure Kotlin representation of a user-granted SAF folder tree.
 */
data class GrantedFolderInfo(
    val treeUri: String,
    val displayName: String,
    val isReadOnly: Boolean,
    val itemCount: Int = 0,
    val isTruncated: Boolean = false,
    val unreadableSubfolders: Int = 0
)

/**
 * Pure Kotlin representation of a removable volume (such as USB OTG or SD card).
 */
data class UsbVolumeInfo(
    val id: String,
    val description: String,
    val state: String,
    val isMounted: Boolean,
    val isRemovable: Boolean,
    val uuid: String?
)
