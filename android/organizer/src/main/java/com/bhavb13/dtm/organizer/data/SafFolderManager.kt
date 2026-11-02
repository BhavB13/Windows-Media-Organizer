package com.bhavb13.dtm.organizer.data

import android.content.ContentResolver
import android.content.Context
import android.content.Intent
import android.database.Cursor
import android.net.Uri
import android.provider.DocumentsContract
import android.webkit.MimeTypeMap
import com.bhavb13.dtm.organizer.core.GrantedFolderInfo
import com.bhavb13.dtm.organizer.core.MediaItem
import com.bhavb13.dtm.organizer.core.MediaKind
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import java.util.Locale
import kotlin.coroutines.coroutineContext

/**
 * Manages Storage Access Framework (SAF) folder grants and document browsing.
 *
 * Safety contract:
 * - Source folders take only READ permissions.
 * - Releases permissions cleanly via [releasePersistableUriPermission].
 * - Queries documents safely and read-only.
 */
class SafFolderManager(private val context: Context) {

    private val resolver: ContentResolver = context.contentResolver

    /**
     * Persists read-only permission for a granted source folder tree.
     */
    fun takePersistableReadPermission(treeUri: Uri): Boolean {
        return try {
            resolver.takePersistableUriPermission(
                treeUri,
                Intent.FLAG_GRANT_READ_URI_PERMISSION
            )
            true
        } catch (_: SecurityException) {
            false
        }
    }

    /**
     * Persists read and write permission for a granted destination folder tree.
     */
    fun takePersistableReadWritePermission(treeUri: Uri): Boolean {
        return try {
            resolver.takePersistableUriPermission(
                treeUri,
                Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION
            )
            true
        } catch (_: SecurityException) {
            false
        }
    }

    /**
     * Releases a previously persisted read permission for a folder tree.
     */
    fun releaseReadPermission(treeUri: Uri): Boolean {
        return try {
            resolver.releasePersistableUriPermission(
                treeUri,
                Intent.FLAG_GRANT_READ_URI_PERMISSION
            )
            true
        } catch (_: SecurityException) {
            false
        }
    }

    /**
     * Lists all persisted folder tree grants held by this app.
     */
    fun listPersistedFolderGrants(): List<GrantedFolderInfo> {
        val persisted = try {
            resolver.persistedUriPermissions
        } catch (_: Exception) {
            return emptyList()
        }

        val results = mutableListOf<GrantedFolderInfo>()
        for (perm in persisted) {
            if (perm.isReadPermission) {
                val displayName = resolveFolderDisplayName(perm.uri)
                results.add(
                    GrantedFolderInfo(
                        treeUri = perm.uri.toString(),
                        displayName = displayName,
                        isReadOnly = !perm.isWritePermission
                    )
                )
            }
        }
        return results
    }

    /**
     * Resolves a human-readable display name for a tree Uri.
     */
    fun resolveFolderDisplayName(treeUri: Uri): String {
        try {
            val rootDocId = DocumentsContract.getTreeDocumentId(treeUri)
            val rootDocUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, rootDocId)
            resolver.query(
                rootDocUri,
                arrayOf(DocumentsContract.Document.COLUMN_DISPLAY_NAME),
                null,
                null,
                null
            )?.use { cursor ->
                if (cursor.moveToFirst()) {
                    val name = cursor.getString(0)
                    if (!name.isNullOrBlank()) return name
                }
            }
            // Fallback to last segment or path in document ID
            val afterColon = rootDocId.substringAfterLast(':')
            val clean = afterColon.substringAfterLast('/').ifBlank { rootDocId }
            return clean.ifBlank { "Folder" }
        } catch (_: Exception) {
            return treeUri.lastPathSegment?.substringAfterLast(':') ?: "Folder"
        }
    }

    /**
     * Browses files in a granted tree via [DocumentsContract] child queries.
     * Recursively walks up to 64 subdirectories off the main thread.
     */
    suspend fun browseTreeFiles(treeUri: Uri): List<MediaItem> = withContext(Dispatchers.IO) {
        val rootDocId = try {
            DocumentsContract.getTreeDocumentId(treeUri)
        } catch (_: Exception) {
            return@withContext emptyList()
        }
        val folderLabel = resolveFolderDisplayName(treeUri)
        val collected = mutableListOf<MediaItem>()
        val queue = ArrayDeque<Pair<String, String>>()
        queue.add(rootDocId to folderLabel)
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
                        val mime = cursor.getStringOrNull(mimeIdx).orEmpty()

                        if (mime == DocumentsContract.Document.MIME_TYPE_DIR) {
                            queue.add(docId to name)
                            continue
                        }

                        val resolvedKind = resolveMediaKind(name, mime) ?: continue
                        val size = cursor.getLongOrZero(sizeIdx)
                        val lastMod = cursor.getLongOrZero(modIdx)
                        val docUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, docId)

                        collected.add(
                            MediaItem(
                                id = docUri.toString(),
                                displayName = name,
                                mimeType = mime.ifBlank { if (resolvedKind == MediaKind.IMAGE) "image/*" else "video/*" },
                                kind = resolvedKind,
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
                // Gracefully skip unreadable subdirectories
            }
        }
        collected
    }

    private fun resolveMediaKind(fileName: String, mimeType: String): MediaKind? {
        MediaKind.fromMimeType(mimeType)?.let { return it }
        val ext = fileName.substringAfterLast('.', "").lowercase(Locale.ROOT)
        if (ext.isEmpty()) return null
        val mappedMime = MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext)
        MediaKind.fromMimeType(mappedMime)?.let { return it }

        return when (ext) {
            "jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "bmp", "dng", "raw" -> MediaKind.IMAGE
            "mp4", "mkv", "mov", "avi", "3gp", "webm", "ts", "m4v" -> MediaKind.VIDEO
            else -> null
        }
    }

    private fun Cursor.getStringOrNull(index: Int): String? {
        return if (index >= 0 && !isNull(index)) getString(index) else null
    }

    private fun Cursor.getLongOrZero(index: Int): Long {
        return if (index >= 0 && !isNull(index)) getLong(index) else 0L
    }
}
