package com.bhavb13.dtm.organizer.core

import java.util.Locale

/**
 * High-level media category supported by the organizer library.
 * Pure Kotlin model with no Android dependencies.
 */
enum class MediaKind(val displayName: String) {
    IMAGE("Images"),
    VIDEO("Videos");

    companion object {
        fun fromMimeType(mimeType: String?): MediaKind? {
            if (mimeType.isNullOrBlank()) return null
            val normalized = mimeType.trim().lowercase(Locale.ROOT)
            return when {
                normalized.startsWith("image/") -> IMAGE
                normalized.startsWith("video/") -> VIDEO
                else -> null
            }
        }
    }
}

/**
 * Immutable metadata snapshot for a single media file in the user's library.
 * Pure Kotlin data class so grouping, ranking, filtering, and aggregation can be tested on the JVM.
 */
data class MediaItem(
    val id: String,
    val displayName: String,
    val mimeType: String,
    val kind: MediaKind,
    val sizeBytes: Long,
    val dateAddedMillis: Long,
    val dateModifiedMillis: Long,
    val captureDateMillis: Long? = null,
    val width: Int? = null,
    val height: Int? = null,
    val durationMillis: Long? = null,
    val cameraMake: String? = null,
    val cameraModel: String? = null,
    val bucketId: String = "",
    val bucketName: String = "Uncategorized",
    val relativePath: String? = null
) {
    /** Total pixel count (width * height), or 0 when dimensions are unavailable or invalid. */
    val pixelCount: Long
        get() {
            val w = width ?: return 0L
            val h = height ?: return 0L
            return if (w > 0 && h > 0) w.toLong() * h.toLong() else 0L
        }

    val hasDimensions: Boolean
        get() = pixelCount > 0L

    val dimensionsLabel: String
        get() = if (hasDimensions) "${width} × ${height}" else "Unknown dimensions"

    val hasCameraMetadata: Boolean
        get() = !cameraMake.isNullOrBlank() || !cameraModel.isNullOrBlank()

    /**
     * Human-readable camera make and model summary, avoiding duplicate prefixes when the model
     * string already starts with the manufacturer name.
     */
    val cameraSummary: String?
        get() {
            val make = cameraMake?.trim().orEmpty()
            val model = cameraModel?.trim().orEmpty()
            return when {
                make.isEmpty() && model.isEmpty() -> null
                make.isEmpty() -> model
                model.isEmpty() -> make
                model.startsWith(make, ignoreCase = true) -> model
                else -> "$make $model"
            }
        }

    /**
     * Best available timestamp (in epoch milliseconds) for chronological sorting and date-range
     * filtering: prefers embedded capture date, then modification date, then date added.
     */
    val effectiveCaptureDateMillis: Long
        get() = when {
            captureDateMillis != null && captureDateMillis > 0L -> captureDateMillis
            dateModifiedMillis > 0L -> dateModifiedMillis
            dateAddedMillis > 0L -> dateAddedMillis
            else -> 0L
        }
}

/**
 * Pure Kotlin byte size formatting helpers.
 */
object ByteFormatting {
    private const val KB = 1024L
    private const val MB = KB * 1024L
    private const val GB = MB * 1024L

    fun formatBytes(bytes: Long): String {
        val safe = bytes.coerceAtLeast(0L)
        return when {
            safe >= GB -> String.format(Locale.US, "%.2f GB", safe.toDouble() / GB.toDouble())
            safe >= MB -> String.format(Locale.US, "%.1f MB", safe.toDouble() / MB.toDouble())
            safe >= KB -> String.format(Locale.US, "%.1f KB", safe.toDouble() / KB.toDouble())
            else -> "$safe B"
        }
    }
}
