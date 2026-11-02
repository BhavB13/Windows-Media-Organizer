package com.bhavb13.dtm.organizer.data

import android.content.ContentResolver
import android.content.ContentUris
import android.content.Context
import android.database.Cursor
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.media.ExifInterface
import android.net.Uri
import android.os.Build
import android.provider.DocumentsContract
import android.provider.MediaStore
import android.util.LruCache
import android.util.Size
import com.bhavb13.dtm.organizer.core.ContentHashSource
import com.bhavb13.dtm.organizer.core.MediaItem
import com.bhavb13.dtm.organizer.core.MediaPaging
import com.bhavb13.dtm.organizer.core.MediaKind
import com.bhavb13.dtm.organizer.core.PermissionStatus
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import java.io.IOException
import java.io.InputStream
import java.text.SimpleDateFormat
import java.util.Locale
import kotlin.coroutines.coroutineContext

/**
 * Read-only repository backed by Android's [MediaStore] and optional read-only Storage Access
 * Framework (SAF) folder grants.
 *
 * Non-negotiable safety guarantee:
 * - Never calls delete, update, insert, or openOutputStream on [ContentResolver] or [DocumentsContract].
 * - Opens media streams strictly in read-only mode for SHA-256 hashing, EXIF inspection, and
 *   thumbnail decoding.
 */
class MediaStoreRepository(private val context: Context) : ContentHashSource {

    private val resolver: ContentResolver = context.contentResolver

    private val thumbnailCache = object : LruCache<String, Bitmap>(80) {}

    override fun openStream(item: MediaItem): InputStream {
        val uri = Uri.parse(item.id)
        return resolver.openInputStream(uri)
            ?: throw IOException("ContentResolver returned null InputStream for ${item.displayName}")
    }

    /**
     * Queries a single page of media items from MediaStore off the main thread.
     *
     * @param permissionStatus current evaluated runtime permission state
     * @param offset zero-based item offset
     * @param limit maximum number of items to return in this page
     */
    suspend fun queryMediaStorePage(
        permissionStatus: PermissionStatus,
        offset: Int,
        limit: Int
    ): List<MediaItem> = withContext(Dispatchers.IO) {
        if (!permissionStatus.canReadImages && !permissionStatus.canReadVideos) {
            return@withContext emptyList()
        }

        val collections = mutableListOf<List<MediaItem>>()
        // Read up to offset + limit rows from each permitted collection, in the same
        // order MediaPaging merges and slices them, so no item can fall between pages.
        val fetchWindow = (offset + limit).coerceAtLeast(limit)

        if (permissionStatus.canReadImages) {
            coroutineContext.ensureActive()
            collections.add(
                queryCollection(
                    collectionUri = MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
                    kind = MediaKind.IMAGE,
                    maxCount = fetchWindow
                )
            )
        }
        if (permissionStatus.canReadVideos) {
            coroutineContext.ensureActive()
            collections.add(
                queryCollection(
                    collectionUri = MediaStore.Video.Media.EXTERNAL_CONTENT_URI,
                    kind = MediaKind.VIDEO,
                    maxCount = fetchWindow
                )
            )
        }

        MediaPaging.page(collections, offset, limit)
    }

    private fun queryCollection(
        collectionUri: Uri,
        kind: MediaKind,
        maxCount: Int
    ): List<MediaItem> {
        val projection = buildProjection(kind)
        val sortOrder = MediaPaging.SQL_SORT_ORDER

        val results = mutableListOf<MediaItem>()
        try {
            resolver.query(collectionUri, projection, null, null, sortOrder)?.use { cursor ->
                val idCol = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns._ID)
                val nameCol = cursor.getColumnIndex(MediaStore.MediaColumns.DISPLAY_NAME)
                val mimeCol = cursor.getColumnIndex(MediaStore.MediaColumns.MIME_TYPE)
                val sizeCol = cursor.getColumnIndex(MediaStore.MediaColumns.SIZE)
                val addedCol = cursor.getColumnIndex(MediaStore.MediaColumns.DATE_ADDED)
                val modCol = cursor.getColumnIndex(MediaStore.MediaColumns.DATE_MODIFIED)
                val takenCol = cursor.getColumnIndex("datetaken")
                val widthCol = cursor.getColumnIndex(MediaStore.MediaColumns.WIDTH)
                val heightCol = cursor.getColumnIndex(MediaStore.MediaColumns.HEIGHT)
                val bucketIdCol = cursor.getColumnIndex("bucket_id")
                val bucketNameCol = cursor.getColumnIndex("bucket_display_name")
                val relPathCol = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                    cursor.getColumnIndex(MediaStore.MediaColumns.RELATIVE_PATH)
                } else {
                    -1
                }
                val durationCol = if (kind == MediaKind.VIDEO) {
                    cursor.getColumnIndex(MediaStore.Video.VideoColumns.DURATION)
                } else {
                    -1
                }

                var count = 0
                while (cursor.moveToNext() && count < maxCount) {
                    val rowId = cursor.getLong(idCol)
                    val contentUri = ContentUris.withAppendedId(collectionUri, rowId)
                    val displayName = cursor.getStringOrNull(nameCol) ?: "media_$rowId"
                    val defaultMime = if (kind == MediaKind.IMAGE) "image/*" else "video/*"
                    val mimeType = cursor.getStringOrNull(mimeCol) ?: defaultMime
                    val sizeBytes = cursor.getLongOrZero(sizeCol)
                    val dateAddedSec = cursor.getLongOrZero(addedCol)
                    val dateModSec = cursor.getLongOrZero(modCol)
                    val dateTakenMillis = cursor.getLongOrNull(takenCol)?.takeIf { it > 0L }
                    val width = cursor.getIntOrNull(widthCol)?.takeIf { it > 0 }
                    val height = cursor.getIntOrNull(heightCol)?.takeIf { it > 0 }
                    val duration = cursor.getLongOrNull(durationCol)?.takeIf { it > 0L }
                    val bucketId = cursor.getStringOrNull(bucketIdCol) ?: "default_bucket"
                    val bucketName = cursor.getStringOrNull(bucketNameCol)?.takeIf { it.isNotBlank() }
                        ?: "Main Storage"
                    val relativePath = cursor.getStringOrNull(relPathCol)

                    results.add(
                        MediaItem(
                            id = contentUri.toString(),
                            displayName = displayName,
                            mimeType = mimeType,
                            kind = kind,
                            sizeBytes = sizeBytes,
                            dateAddedMillis = dateAddedSec * 1000L,
                            dateModifiedMillis = dateModSec * 1000L,
                            captureDateMillis = dateTakenMillis,
                            width = width,
                            height = height,
                            durationMillis = duration,
                            cameraMake = null,
                            cameraModel = null,
                            bucketId = bucketId,
                            bucketName = bucketName,
                            relativePath = relativePath
                        )
                    )
                    count++
                }
            }
        } catch (_: SecurityException) {
            // Gracefully handle revoked permissions without crashing
        } catch (_: Exception) {
            // Gracefully handle provider errors without crashing
        }
        return results
    }

    private fun buildProjection(kind: MediaKind): Array<String> {
        val cols = mutableListOf(
            MediaStore.MediaColumns._ID,
            MediaStore.MediaColumns.DISPLAY_NAME,
            MediaStore.MediaColumns.MIME_TYPE,
            MediaStore.MediaColumns.SIZE,
            MediaStore.MediaColumns.DATE_ADDED,
            MediaStore.MediaColumns.DATE_MODIFIED,
            MediaStore.MediaColumns.WIDTH,
            MediaStore.MediaColumns.HEIGHT,
            "datetaken",
            "bucket_id",
            "bucket_display_name"
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            cols.add(MediaStore.MediaColumns.RELATIVE_PATH)
        }
        if (kind == MediaKind.VIDEO) {
            cols.add(MediaStore.Video.VideoColumns.DURATION)
        }
        return cols.toTypedArray()
    }

    /**
     * Reads supplemental EXIF metadata (camera make/model, dimensions, capture timestamp) for an
     * image off the main thread without modifying the file.
     */
    suspend fun enrichItemMetadata(item: MediaItem): MediaItem = withContext(Dispatchers.IO) {
        if (item.kind != MediaKind.IMAGE) return@withContext item
        try {
            val uri = Uri.parse(item.id)
            resolver.openInputStream(uri)?.use { stream ->
                val exif = ExifInterface(stream)
                val make = exif.getAttribute(ExifInterface.TAG_MAKE)?.trim()?.takeIf { it.isNotEmpty() }
                val model = exif.getAttribute(ExifInterface.TAG_MODEL)?.trim()?.takeIf { it.isNotEmpty() }
                val exifWidth = exif.getAttributeInt(ExifInterface.TAG_IMAGE_WIDTH, 0).takeIf { it > 0 }
                val exifHeight = exif.getAttributeInt(ExifInterface.TAG_IMAGE_LENGTH, 0).takeIf { it > 0 }
                val exifDateStr = exif.getAttribute(ExifInterface.TAG_DATETIME_ORIGINAL)
                    ?: exif.getAttribute(ExifInterface.TAG_DATETIME)
                val parsedExifMillis = parseExifDateMillis(exifDateStr)

                item.copy(
                    cameraMake = item.cameraMake ?: make,
                    cameraModel = item.cameraModel ?: model,
                    width = item.width ?: exifWidth,
                    height = item.height ?: exifHeight,
                    captureDateMillis = item.captureDateMillis ?: parsedExifMillis
                )
            } ?: item
        } catch (_: Exception) {
            item
        }
    }

    private fun parseExifDateMillis(raw: String?): Long? {
        if (raw.isNullOrBlank()) return null
        return try {
            val parser = SimpleDateFormat("yyyy:MM:dd HH:mm:ss", Locale.US)
            parser.parse(raw.trim())?.time
        } catch (_: Exception) {
            null
        }
    }

    /**
     * Enumerates media files inside a read-only SAF folder tree granted by the user.
     */
    suspend fun scanSafFolderTreeReadOnly(treeUri: Uri): List<MediaItem> = withContext(Dispatchers.IO) {
        val rootDocId = try {
            DocumentsContract.getTreeDocumentId(treeUri)
        } catch (_: Exception) {
            return@withContext emptyList()
        }
        val bucketLabel = rootDocId.substringAfterLast(':').substringAfterLast('/').ifBlank { "SAF Folder" }
        val collected = mutableListOf<MediaItem>()
        val queue = ArrayDeque<Pair<String, String>>()
        queue.add(rootDocId to bucketLabel)
        var visitedDirs = 0

        while (queue.isNotEmpty() && visitedDirs < 64) {
            coroutineContext.ensureActive()
            val (parentDocId, currentBucket) = queue.removeFirst()
            visitedDirs++

            val childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, parentDocId)
            val projection = arrayOf(
                DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                DocumentsContract.Document.COLUMN_MIME_TYPE,
                DocumentsContract.Document.COLUMN_SIZE,
                DocumentsContract.Document.COLUMN_LAST_MODIFIED
            )

            try {
                resolver.query(childrenUri, projection, null, null, null)?.use { cursor ->
                    val idIdx = cursor.getColumnIndex(DocumentsContract.Document.COLUMN_DOCUMENT_ID)
                    val nameIdx = cursor.getColumnIndex(DocumentsContract.Document.COLUMN_DISPLAY_NAME)
                    val mimeIdx = cursor.getColumnIndex(DocumentsContract.Document.COLUMN_MIME_TYPE)
                    val sizeIdx = cursor.getColumnIndex(DocumentsContract.Document.COLUMN_SIZE)
                    val modIdx = cursor.getColumnIndex(DocumentsContract.Document.COLUMN_LAST_MODIFIED)

                    while (cursor.moveToNext()) {
                        val docId = cursor.getStringOrNull(idIdx) ?: continue
                        val name = cursor.getStringOrNull(nameIdx) ?: docId
                        val mime = cursor.getStringOrNull(mimeIdx) ?: ""
                        if (mime == DocumentsContract.Document.MIME_TYPE_DIR) {
                            queue.add(docId to name)
                            continue
                        }
                        val kind = MediaKind.fromMimeType(mime) ?: continue
                        val size = cursor.getLongOrZero(sizeIdx)
                        val lastMod = cursor.getLongOrZero(modIdx)
                        val docUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, docId)

                        collected.add(
                            MediaItem(
                                id = docUri.toString(),
                                displayName = name,
                                mimeType = mime,
                                kind = kind,
                                sizeBytes = size,
                                dateAddedMillis = lastMod,
                                dateModifiedMillis = lastMod,
                                captureDateMillis = lastMod.takeIf { it > 0L },
                                width = null,
                                height = null,
                                durationMillis = null,
                                cameraMake = null,
                                cameraModel = null,
                                bucketId = "saf:$currentBucket",
                                bucketName = "SAF: $currentBucket",
                                relativePath = docId
                            )
                        )
                    }
                }
            } catch (_: Exception) {
                // Skip unreadable subfolder gracefully
            }
        }
        collected
    }

    /**
     * Loads a small thumbnail bitmap off the main thread, caching results in memory so the Compose
     * grid never stutters or blocks the UI thread.
     */
    suspend fun loadThumbnail(item: MediaItem, targetSizePx: Int = 240): Bitmap? = withContext(Dispatchers.IO) {
        thumbnailCache.get(item.id)?.let { return@withContext it }

        val uri = try {
            Uri.parse(item.id)
        } catch (_: Exception) {
            return@withContext null
        }

        val bitmap: Bitmap? = try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                resolver.loadThumbnail(uri, Size(targetSizePx, targetSizePx), null)
            } else if (item.kind == MediaKind.IMAGE) {
                decodeSampledBitmap(uri, targetSizePx)
            } else {
                null
            }
        } catch (_: Exception) {
            null
        }

        if (bitmap != null) {
            thumbnailCache.put(item.id, bitmap)
        }
        bitmap
    }

    private fun decodeSampledBitmap(uri: Uri, reqSize: Int): Bitmap? {
        val boundsOptions = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        resolver.openInputStream(uri)?.use { stream ->
            BitmapFactory.decodeStream(stream, null, boundsOptions)
        }
        val rawW = boundsOptions.outWidth
        val rawH = boundsOptions.outHeight
        if (rawW <= 0 || rawH <= 0) return null

        var inSampleSize = 1
        while (rawW / inSampleSize > reqSize * 2 && rawH / inSampleSize > reqSize * 2) {
            inSampleSize *= 2
        }

        val decodeOptions = BitmapFactory.Options().apply {
            this.inSampleSize = inSampleSize
            this.inPreferredConfig = Bitmap.Config.RGB_565
        }
        return resolver.openInputStream(uri)?.use { stream ->
            BitmapFactory.decodeStream(stream, null, decodeOptions)
        }
    }

    private fun Cursor.getStringOrNull(index: Int): String? {
        return if (index >= 0 && !isNull(index)) getString(index) else null
    }

    private fun Cursor.getLongOrZero(index: Int): Long {
        return if (index >= 0 && !isNull(index)) getLong(index) else 0L
    }

    private fun Cursor.getLongOrNull(index: Int): Long? {
        return if (index >= 0 && !isNull(index)) getLong(index) else null
    }

    private fun Cursor.getIntOrNull(index: Int): Int? {
        return if (index >= 0 && !isNull(index)) getInt(index) else null
    }
}
