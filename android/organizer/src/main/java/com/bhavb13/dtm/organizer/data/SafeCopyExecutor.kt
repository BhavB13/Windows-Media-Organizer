package com.bhavb13.dtm.organizer.data

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract
import com.bhavb13.dtm.organizer.core.CopyVerifier
import com.bhavb13.dtm.organizer.core.ExistingDestinationFile
import com.bhavb13.dtm.organizer.core.OrganizeItemFailure
import com.bhavb13.dtm.organizer.core.OrganizePlan
import com.bhavb13.dtm.organizer.core.OrganizeProgress
import com.bhavb13.dtm.organizer.core.OrganizeResult
import com.bhavb13.dtm.organizer.core.PlannedAction
import com.bhavb13.dtm.organizer.core.Sha256Hasher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.security.MessageDigest
import kotlin.coroutines.coroutineContext

/**
 * Executes a verified safe organize copy plan off the main thread.
 *
 * Non-negotiable safety rules:
 * - SOURCE MEDIA IS SACRED: Never write to, move, rename, or delete a source file.
 * - COPIES ARE COPY-ONLY into the destination.
 * - Every written document is verified by SHA-256 against the source.
 * - On mismatch or cancellation mid-write, delete ONLY the just-created destination document.
 */
class SafeCopyExecutor(private val context: Context) {

    private val resolver: ContentResolver = context.contentResolver

    companion object {
        private const val BUFFER_SIZE = 64 * 1024
    }

    /**
     * Executes the given [plan] with cooperative cancellation and progress emission.
     */
    suspend fun execute(
        plan: OrganizePlan,
        onProgress: (OrganizeProgress) -> Unit = {},
        isCancelled: () -> Boolean = { false }
    ): OrganizeResult = withContext(Dispatchers.IO) {
        if (isCancelled()) {
            return@withContext OrganizeResult.cancelled(
                totalPlanned = plan.items.size,
                copiedCount = 0,
                skippedCount = 0
            )
        }

        val destinationTreeUri = Uri.parse(plan.destinationTreeUri)
        val dirUriCache = mutableMapOf<String, Uri>()
        val failures = mutableListOf<OrganizeItemFailure>()
        var copiedCount = 0
        var skippedCount = 0
        var bytesCopied = 0L

        val totalToProcess = plan.items.size

        for ((index, plannedItem) in plan.items.withIndex()) {
            if (isCancelled()) {
                return@withContext OrganizeResult.cancelled(
                    totalPlanned = totalToProcess,
                    copiedCount = copiedCount,
                    skippedCount = skippedCount,
                    failures = failures
                )
            }

            val sourceItem = plannedItem.sourceItem

            // Case 1: Skipped because identical content already exists at destination
            if (plannedItem.action == PlannedAction.SKIP_IDENTICAL) {
                skippedCount++
                onProgress(
                    OrganizeProgress(
                        completedCount = index + 1,
                        totalCount = totalToProcess,
                        currentFileName = plannedItem.targetFileName,
                        bytesCopied = bytesCopied,
                        totalBytesToCopy = plan.totalCopyBytes,
                        phase = "Skipped identical: ${plannedItem.targetFileName}"
                    )
                )
                continue
            }

            // Case 2: Copy file (COPY or COPY_WITH_RENAME)
            onProgress(
                OrganizeProgress(
                    completedCount = index,
                    totalCount = totalToProcess,
                    currentFileName = plannedItem.targetFileName,
                    bytesCopied = bytesCopied,
                    totalBytesToCopy = plan.totalCopyBytes,
                    phase = "Copying ${plannedItem.targetFileName}"
                )
            )

            // Resolve or create target subfolder inside destination tree
            val targetDirUri = try {
                getOrCreateSubfolder(destinationTreeUri, plannedItem.destinationSubfolder, dirUriCache)
            } catch (e: Exception) {
                failures.add(
                    OrganizeItemFailure(
                        item = sourceItem,
                        targetFileName = plannedItem.targetFileName,
                        targetSubfolder = plannedItem.destinationSubfolder,
                        errorMessage = "Could not create target folder ${plannedItem.destinationSubfolder}: ${e.message}"
                    )
                )
                continue
            }

            // Create target document in destination
            var createdDocUri: Uri? = null
            try {
                createdDocUri = DocumentsContract.createDocument(
                    resolver,
                    targetDirUri,
                    sourceItem.mimeType,
                    plannedItem.targetFileName
                )
                if (createdDocUri == null) {
                    throw IOException("Destination provider refused creation of ${plannedItem.targetFileName}")
                }

                // Stream copy and calculate source SHA-256 simultaneously
                val sourceUri = Uri.parse(sourceItem.id)
                val sourceDigest = MessageDigest.getInstance("SHA-256")

                resolver.openInputStream(sourceUri).use { inStream ->
                    if (inStream == null) throw IOException("Could not open source stream for ${sourceItem.displayName}")
                    resolver.openOutputStream(createdDocUri, "w").use { outStream ->
                        if (outStream == null) throw IOException("Could not open destination stream for ${plannedItem.targetFileName}")

                        val buffer = ByteArray(BUFFER_SIZE)
                        while (true) {
                            if (isCancelled()) {
                                // Clean up incomplete document
                                safeDeleteDestinationDocument(createdDocUri)
                                return@withContext OrganizeResult.cancelled(
                                    totalPlanned = totalToProcess,
                                    copiedCount = copiedCount,
                                    skippedCount = skippedCount,
                                    failures = failures
                                )
                            }
                            val read = inStream.read(buffer)
                            if (read < 0) break
                            if (read > 0) {
                                sourceDigest.update(buffer, 0, read)
                                outStream.write(buffer, 0, read)
                            }
                        }
                        outStream.flush()
                    }
                }

                val sourceSha256Hex = Sha256Hasher.toHex(sourceDigest.digest())

                // Verify by reading back destination document
                onProgress(
                    OrganizeProgress(
                        completedCount = index,
                        totalCount = totalToProcess,
                        currentFileName = plannedItem.targetFileName,
                        bytesCopied = bytesCopied,
                        totalBytesToCopy = plan.totalCopyBytes,
                        phase = "Verifying ${plannedItem.targetFileName} (SHA-256)"
                    )
                )

                val destSha256Hex = resolver.openInputStream(createdDocUri)?.use { destStream ->
                    Sha256Hasher.hashStream(destStream, isCancelled)
                }

                if (destSha256Hex == null || isCancelled()) {
                    safeDeleteDestinationDocument(createdDocUri)
                    return@withContext OrganizeResult.cancelled(
                        totalPlanned = totalToProcess,
                        copiedCount = copiedCount,
                        skippedCount = skippedCount,
                        failures = failures
                    )
                }

                val verification = CopyVerifier.verify(sourceSha256Hex, destSha256Hex)
                when (verification) {
                    is CopyVerifier.VerificationOutcome.Match -> {
                        // Success!
                        copiedCount++
                        bytesCopied += sourceItem.sizeBytes.coerceAtLeast(0L)
                    }
                    is CopyVerifier.VerificationOutcome.Mismatch -> {
                        // Delete unverified document immediately
                        safeDeleteDestinationDocument(createdDocUri)
                        failures.add(
                            OrganizeItemFailure(
                                item = sourceItem,
                                targetFileName = plannedItem.targetFileName,
                                targetSubfolder = plannedItem.destinationSubfolder,
                                errorMessage = "Verification failed: ${verification.description}. Destination document deleted."
                            )
                        )
                    }
                }
            } catch (e: Exception) {
                // If anything failed, clean up the just-created destination document ONLY
                createdDocUri?.let { safeDeleteDestinationDocument(it) }
                failures.add(
                    OrganizeItemFailure(
                        item = sourceItem,
                        targetFileName = plannedItem.targetFileName,
                        targetSubfolder = plannedItem.destinationSubfolder,
                        errorMessage = e.message ?: "Failed to copy file"
                    )
                )
            }

            onProgress(
                OrganizeProgress(
                    completedCount = index + 1,
                    totalCount = totalToProcess,
                    currentFileName = plannedItem.targetFileName,
                    bytesCopied = bytesCopied,
                    totalBytesToCopy = plan.totalCopyBytes,
                    phase = "Completed ${index + 1} of $totalToProcess"
                )
            )
        }

        OrganizeResult(
            totalPlanned = totalToProcess,
            copiedCount = copiedCount,
            skippedCount = skippedCount,
            failureCount = failures.size,
            failures = failures,
            wasCancelled = false
        )
    }

    /**
     * Finds or creates a nested subfolder (e.g. "2026/11" or "2026/11/02") inside the destination tree.
     */
    fun getOrCreateSubfolder(
        treeUri: Uri,
        subfolderPath: String,
        dirCache: MutableMap<String, Uri>
    ): Uri {
        val rootDocId = DocumentsContract.getTreeDocumentId(treeUri)
        val rootDocUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, rootDocId)

        if (subfolderPath.isBlank() || subfolderPath == ".") {
            return rootDocUri
        }

        dirCache[subfolderPath]?.let { return it }

        val segments = subfolderPath.split('/').filter { it.isNotBlank() }
        var currentDirUri = rootDocUri
        var currentSubpath = ""

        for (segment in segments) {
            currentSubpath = if (currentSubpath.isEmpty()) segment else "$currentSubpath/$segment"
            dirCache[currentSubpath]?.let {
                currentDirUri = it
                return@let
            }

            val existing = findChildDirectory(currentDirUri, segment)
            if (existing != null) {
                currentDirUri = existing
            } else {
                val created = DocumentsContract.createDocument(
                    resolver,
                    currentDirUri,
                    DocumentsContract.Document.MIME_TYPE_DIR,
                    segment
                ) ?: throw IOException("Could not create directory '$segment' under $currentSubpath")
                currentDirUri = created
            }
            dirCache[currentSubpath] = currentDirUri
        }

        return currentDirUri
    }

    private fun findChildDirectory(parentUri: Uri, displayName: String): Uri? {
        val treeUri = parentUri
        val parentDocId = DocumentsContract.getDocumentId(parentUri)
        val childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, parentDocId)

        resolver.query(
            childrenUri,
            arrayOf(
                DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                DocumentsContract.Document.COLUMN_MIME_TYPE
            ),
            null,
            null,
            null
        )?.use { cursor ->
            val idCol = cursor.getColumnIndex(DocumentsContract.Document.COLUMN_DOCUMENT_ID)
            val nameCol = cursor.getColumnIndex(DocumentsContract.Document.COLUMN_DISPLAY_NAME)
            val mimeCol = cursor.getColumnIndex(DocumentsContract.Document.COLUMN_MIME_TYPE)

            while (cursor.moveToNext()) {
                val docId = cursor.getString(idCol) ?: continue
                val name = cursor.getString(nameCol) ?: continue
                val mime = cursor.getString(mimeCol) ?: ""
                if (mime == DocumentsContract.Document.MIME_TYPE_DIR && name.equals(displayName, ignoreCase = true)) {
                    return DocumentsContract.buildDocumentUriUsingTree(treeUri, docId)
                }
            }
        }
        return null
    }

    /**
     * Lists existing files inside a subfolder in the destination tree for planning.
     */
    fun listExistingFilesInSubfolder(treeUri: Uri, subfolder: String): List<ExistingDestinationFile> {
        val rootDocId = DocumentsContract.getTreeDocumentId(treeUri)
        val rootDocUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, rootDocId)

        var dirUri: Uri = rootDocUri
        if (subfolder.isNotBlank()) {
            val segments = subfolder.split('/').filter { it.isNotBlank() }
            for (segment in segments) {
                val next = findChildDirectory(dirUri, segment) ?: return emptyList()
                dirUri = next
            }
        }

        val parentDocId = DocumentsContract.getDocumentId(dirUri)
        val childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(treeUri, parentDocId)

        val results = mutableListOf<ExistingDestinationFile>()
        resolver.query(
            childrenUri,
            arrayOf(
                DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                DocumentsContract.Document.COLUMN_MIME_TYPE,
                DocumentsContract.Document.COLUMN_SIZE
            ),
            null,
            null,
            null
        )?.use { cursor ->
            val idCol = cursor.getColumnIndex(DocumentsContract.Document.COLUMN_DOCUMENT_ID)
            val nameCol = cursor.getColumnIndex(DocumentsContract.Document.COLUMN_DISPLAY_NAME)
            val mimeCol = cursor.getColumnIndex(DocumentsContract.Document.COLUMN_MIME_TYPE)
            val sizeCol = cursor.getColumnIndex(DocumentsContract.Document.COLUMN_SIZE)

            while (cursor.moveToNext()) {
                val docId = cursor.getString(idCol) ?: continue
                val name = cursor.getString(nameCol) ?: continue
                val mime = cursor.getString(mimeCol) ?: ""
                if (mime != DocumentsContract.Document.MIME_TYPE_DIR) {
                    val size = cursor.getLong(sizeCol)
                    results.add(
                        ExistingDestinationFile(
                            fileName = name,
                            subfolder = subfolder,
                            sizeBytes = size,
                            sha256 = null,
                            documentUri = DocumentsContract.buildDocumentUriUsingTree(treeUri, docId).toString()
                        )
                    )
                }
            }
        }
        return results
    }

    /**
     * Hashes an existing document on demand.
     */
    fun hashExistingDocument(docUri: Uri): String? {
        return try {
            resolver.openInputStream(docUri)?.use { stream ->
                Sha256Hasher.hashStream(stream)
            }
        } catch (_: Exception) {
            null
        }
    }

    private fun safeDeleteDestinationDocument(docUri: Uri) {
        try {
            DocumentsContract.deleteDocument(resolver, docUri)
        } catch (_: Exception) {
            // Ignore failure when deleting temporary document
        }
    }
}
