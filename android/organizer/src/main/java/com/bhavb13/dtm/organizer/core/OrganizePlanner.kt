package com.bhavb13.dtm.organizer.core

import java.util.TimeZone

/**
 * Interface to inspect existing files in the destination folder during planning.
 */
fun interface DestinationFileProvider {
    fun findExistingFile(subfolder: String, fileName: String): ExistingDestinationFile?
}

/**
 * Pure Kotlin organizer planner.
 *
 * Safety contract:
 * - Deterministic, read-only planning: never touches, creates, or deletes any files.
 * - Categorizes each item into COPY, SKIP_IDENTICAL, or COPY_WITH_RENAME.
 * - Resolves collisions using "name (1).ext", "name (2).ext", etc.
 * - Prevents name collisions across multiple source files within the same batch.
 */
object OrganizePlanner {

    fun plan(
        items: List<MediaItem>,
        template: OrganizeTemplate,
        destinationTreeUri: String,
        destinationLabel: String,
        destinationFiles: DestinationFileProvider = DestinationFileProvider { _, _ -> null },
        sourceSha256Provider: (MediaItem) -> String? = { null },
        destSha256Provider: (ExistingDestinationFile) -> String? = { null },
        timeZone: TimeZone = TimeZone.getDefault()
    ): OrganizePlan {
        val plannedItems = mutableListOf<PlannedCopyItem>()
        // Names already claimed by earlier items in this batch, per subfolder (lowercased),
        // mapped to the item that claimed them. A claim counts even when its hash is unknown.
        val claimedBySubfolder = mutableMapOf<String, MutableMap<String, MediaItem>>()
        // Hashing reads whole files, so only hash when a name actually collides, and only once.
        val sourceHashes = mutableMapOf<String, String?>()
        fun sourceSha(item: MediaItem): String? = sourceHashes.getOrPut(item.id) { sourceSha256Provider(item) }

        for (item in items) {
            val subfolder = template.render(
                captureDateMillis = item.captureDateMillis,
                dateModifiedMillis = item.dateModifiedMillis,
                timeZone = timeZone
            )

            val claimed = claimedBySubfolder.getOrPut(subfolder) { mutableMapOf() }
            val originalName = item.displayName.ifBlank { "media_${item.id.hashCode()}" }

            var candidateName = originalName
            var candidateIndex = 0
            var action: PlannedAction
            var reason: String
            var matchedSourceSha: String? = null
            var matchedDestSha: String? = null

            while (true) {
                val candidateKey = candidateName.lowercase()
                val claimedBy = claimed[candidateKey]
                val existingInDest = if (claimedBy == null) destinationFiles.findExistingFile(subfolder, candidateName) else null

                if (claimedBy == null && existingInDest == null) {
                    action = if (candidateIndex == 0) PlannedAction.COPY else PlannedAction.COPY_WITH_RENAME
                    reason = if (candidateIndex == 0) {
                        "Safe copy to $subfolder/$candidateName"
                    } else {
                        "Name collision resolved to $subfolder/$candidateName"
                    }
                    claimed[candidateKey] = item
                    break
                }

                // Skip only when both hashes are known and equal; an unknown hash is a conflict.
                val existingSha = if (claimedBy != null) {
                    sourceSha(claimedBy)
                } else {
                    existingInDest!!.sha256 ?: destSha256Provider(existingInDest)
                }
                val itemSha = if (existingSha != null) sourceSha(item) else null

                if (itemSha != null && itemSha.equals(existingSha, ignoreCase = true)) {
                    action = PlannedAction.SKIP_IDENTICAL
                    reason = "Identical content already exists at destination ($subfolder/$candidateName)"
                    matchedSourceSha = itemSha
                    matchedDestSha = existingSha
                    break
                }

                candidateIndex++
                candidateName = ConflictNaming.generateCandidateName(originalName, candidateIndex)
            }

            plannedItems.add(
                PlannedCopyItem(
                    sourceItem = item,
                    destinationSubfolder = subfolder,
                    targetFileName = candidateName,
                    action = action,
                    sourceSha256 = matchedSourceSha,
                    existingDestinationSha256 = matchedDestSha,
                    reason = reason
                )
            )
        }

        val toCopy = plannedItems.filter { it.action != PlannedAction.SKIP_IDENTICAL }
        val toSkip = plannedItems.filter { it.action == PlannedAction.SKIP_IDENTICAL }
        val conflicts = plannedItems.filter { it.action == PlannedAction.COPY_WITH_RENAME }
        val totalBytes = toCopy.sumOf { it.sourceItem.sizeBytes.coerceAtLeast(0L) }
        val uniqueFolders = plannedItems.map { it.destinationSubfolder }.distinct().sorted()

        return OrganizePlan(
            template = template,
            destinationTreeUri = destinationTreeUri,
            destinationLabel = destinationLabel,
            items = plannedItems,
            totalSourceCount = items.size,
            toCopyCount = toCopy.size,
            toSkipCount = toSkip.size,
            conflictCount = conflicts.size,
            totalCopyBytes = totalBytes,
            destinationFolders = uniqueFolders
        )
    }
}
