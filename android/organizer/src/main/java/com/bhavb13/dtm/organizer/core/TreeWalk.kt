package com.bhavb13.dtm.organizer.core

/**
 * A directory node visited during tree walk.
 *
 * @param id Unique identifier (e.g. SAF Document ID) used to prevent revisiting.
 * @param name Display or bucket name for this directory.
 */
data class TreeFolder(
    val id: String,
    val name: String
)

/**
 * Result of querying children for a single folder in [TreeWalk].
 */
sealed class FolderListing<out T> {
    /**
     * Successfully read folder children.
     */
    data class Success<T>(
        val subfolders: List<TreeFolder> = emptyList(),
        val files: List<T> = emptyList()
    ) : FolderListing<T>()

    /**
     * Folder could not be read (e.g. SecurityException, cursor null, I/O error).
     */
    data class Unreadable(val errorMessage: String? = null) : FolderListing<Nothing>()
}

/**
 * Outcome of a breadth-first tree walk.
 */
data class TreeWalkResult<T>(
    val items: List<T>,
    val truncated: Boolean,
    val foldersVisited: Int,
    val unreadableSubfolders: Int = 0,
    val wasCancelled: Boolean = false
) {
    val totalFilesFound: Int get() = items.size
}

/**
 * Pure-Kotlin breadth-first directory tree walker.
 *
 * Safety & correctness contracts:
 * - Visits folders breadth-first.
 * - Guards against circular loops by document ID.
 * - Enforces folder visit limit (default 2,000) and file limit (default 50,000).
 * - Flags truncation if limits are hit with remaining unvisited work.
 * - Counts unreadable subfolders rather than silently dropping them.
 * - Supports cooperative cancellation via [isCancelled].
 */
object TreeWalk {
    const val DEFAULT_MAX_FOLDERS = 2_000
    const val DEFAULT_MAX_FILES = 50_000

    fun <T> walk(
        rootFolder: TreeFolder,
        maxFolders: Int = DEFAULT_MAX_FOLDERS,
        maxFiles: Int = DEFAULT_MAX_FILES,
        isCancelled: () -> Boolean = { false },
        listChildren: (TreeFolder) -> FolderListing<T>
    ): TreeWalkResult<T> {
        if (isCancelled()) {
            return TreeWalkResult(
                items = emptyList(),
                truncated = false,
                foldersVisited = 0,
                unreadableSubfolders = 0,
                wasCancelled = true
            )
        }

        if (maxFolders <= 0 || maxFiles <= 0) {
            return TreeWalkResult(
                items = emptyList(),
                truncated = true,
                foldersVisited = 0,
                unreadableSubfolders = 0,
                wasCancelled = false
            )
        }

        val queue = ArrayDeque<TreeFolder>()
        val visitedFolderIds = mutableSetOf<String>()
        val collectedItems = mutableListOf<T>()
        var foldersVisited = 0
        var unreadableSubfolders = 0
        var truncated = false

        visitedFolderIds.add(rootFolder.id)
        queue.add(rootFolder)

        while (queue.isNotEmpty()) {
            if (isCancelled()) {
                return TreeWalkResult(
                    items = collectedItems,
                    truncated = truncated,
                    foldersVisited = foldersVisited,
                    unreadableSubfolders = unreadableSubfolders,
                    wasCancelled = true
                )
            }

            if (foldersVisited >= maxFolders) {
                truncated = true
                break
            }

            val currentFolder = queue.removeFirst()
            foldersVisited++

            val listing = try {
                listChildren(currentFolder)
            } catch (t: Throwable) {
                FolderListing.Unreadable(t.message)
            }

            when (listing) {
                is FolderListing.Unreadable -> {
                    unreadableSubfolders++
                }
                is FolderListing.Success -> {
                    for (file in listing.files) {
                        if (collectedItems.size < maxFiles) {
                            collectedItems.add(file)
                        } else {
                            truncated = true
                            break
                        }
                    }

                    if (truncated) {
                        break
                    }

                    for (subfolder in listing.subfolders) {
                        if (visitedFolderIds.add(subfolder.id)) {
                            queue.add(subfolder)
                        }
                    }

                    if (collectedItems.size >= maxFiles && queue.isNotEmpty()) {
                        truncated = true
                        break
                    }
                }
            }
        }

        return TreeWalkResult(
            items = collectedItems,
            truncated = truncated,
            foldersVisited = foldersVisited,
            unreadableSubfolders = unreadableSubfolders,
            wasCancelled = false
        )
    }
}
