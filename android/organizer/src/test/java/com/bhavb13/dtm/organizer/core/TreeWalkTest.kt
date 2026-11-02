package com.bhavb13.dtm.organizer.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TreeWalkTest {

    @Test
    fun defaultLimits_are2000FoldersAnd50000Files() {
        assertEquals(2_000, TreeWalk.DEFAULT_MAX_FOLDERS)
        assertEquals(50_000, TreeWalk.DEFAULT_MAX_FILES)
    }

    @Test
    fun singleFolderWithFiles_completesWithoutTruncation() {
        val root = TreeFolder(id = "root", name = "DCIM")
        val result = TreeWalk.walk(
            rootFolder = root,
            listChildren = { folder ->
                assertEquals("root", folder.id)
                FolderListing.Success(
                    subfolders = emptyList(),
                    files = listOf("photo1.jpg", "photo2.jpg", "video1.mp4")
                )
            }
        )

        assertFalse(result.truncated)
        assertFalse(result.wasCancelled)
        assertEquals(1, result.foldersVisited)
        assertEquals(0, result.unreadableSubfolders)
        assertEquals(3, result.items.size)
        assertEquals(listOf("photo1.jpg", "photo2.jpg", "video1.mp4"), result.items)
    }

    @Test
    fun breadthFirstOrder_visitsLevelByLevel() {
        // Hierarchy:
        // root
        // ├── folderA
        // │   └── folderA1
        // └── folderB
        val root = TreeFolder("root", "Root")
        val fA = TreeFolder("folderA", "A")
        val fB = TreeFolder("folderB", "B")
        val fA1 = TreeFolder("folderA1", "A1")

        val visitedOrder = mutableListOf<String>()

        val result = TreeWalk.walk(
            rootFolder = root,
            listChildren = { folder ->
                visitedOrder.add(folder.id)
                when (folder.id) {
                    "root" -> FolderListing.Success(subfolders = listOf(fA, fB), files = listOf("root_file.jpg"))
                    "folderA" -> FolderListing.Success(subfolders = listOf(fA1), files = listOf("a_file.jpg"))
                    "folderB" -> FolderListing.Success(subfolders = emptyList(), files = listOf("b_file.jpg"))
                    "folderA1" -> FolderListing.Success(subfolders = emptyList(), files = listOf("a1_file.jpg"))
                    else -> FolderListing.Success()
                }
            }
        )

        assertEquals(listOf("root", "folderA", "folderB", "folderA1"), visitedOrder)
        assertEquals(4, result.foldersVisited)
        assertFalse(result.truncated)
        assertEquals(4, result.items.size)
    }

    @Test
    fun circularFolderReference_isGuardedByDocumentId() {
        // root has child; child has root and itself
        val root = TreeFolder("root", "Root")
        val child = TreeFolder("child", "Child")

        val visitCount = mutableMapOf<String, Int>()

        val result = TreeWalk.walk(
            rootFolder = root,
            listChildren = { folder ->
                visitCount[folder.id] = (visitCount[folder.id] ?: 0) + 1
                when (folder.id) {
                    "root" -> FolderListing.Success(subfolders = listOf(child), files = listOf("root.jpg"))
                    "child" -> FolderListing.Success(subfolders = listOf(root, child), files = listOf("child.jpg"))
                    else -> FolderListing.Success()
                }
            }
        )

        assertEquals(1, visitCount["root"])
        assertEquals(1, visitCount["child"])
        assertEquals(2, result.foldersVisited)
        assertFalse(result.truncated)
        assertEquals(2, result.items.size)
    }

    @Test
    fun folderLimitExceeded_flagsTruncatedAndHalts() {
        val root = TreeFolder("root", "Root")
        val folders = (1..10).map { TreeFolder("sub$it", "Sub $it") }

        val result = TreeWalk.walk(
            rootFolder = root,
            maxFolders = 4,
            listChildren = { folder ->
                if (folder.id == "root") {
                    FolderListing.Success(subfolders = folders)
                } else {
                    FolderListing.Success(subfolders = emptyList(), files = listOf("${folder.id}.jpg"))
                }
            }
        )

        assertTrue("Expected walk to be marked truncated", result.truncated)
        assertEquals(4, result.foldersVisited)
        assertEquals(3, result.items.size)
    }

    @Test
    fun fileLimitExceeded_flagsTruncatedAndHalts() {
        val root = TreeFolder("root", "Root")
        val f1 = TreeFolder("f1", "F1")
        val f2 = TreeFolder("f2", "F2")

        val result = TreeWalk.walk(
            rootFolder = root,
            maxFiles = 5,
            listChildren = { folder ->
                when (folder.id) {
                    "root" -> FolderListing.Success(subfolders = listOf(f1, f2), files = listOf("1.jpg", "2.jpg", "3.jpg"))
                    "f1" -> FolderListing.Success(subfolders = emptyList(), files = listOf("4.jpg", "5.jpg", "6.jpg", "7.jpg"))
                    "f2" -> FolderListing.Success(subfolders = emptyList(), files = listOf("8.jpg"))
                    else -> FolderListing.Success()
                }
            }
        )

        assertTrue(result.truncated)
        assertEquals(5, result.items.size)
        assertEquals(listOf("1.jpg", "2.jpg", "3.jpg", "4.jpg", "5.jpg"), result.items)
    }

    @Test
    fun exactFileLimitReached_withRemainingSubfolders_marksTruncated() {
        val root = TreeFolder("root", "Root")
        val f1 = TreeFolder("f1", "F1")

        val result = TreeWalk.walk(
            rootFolder = root,
            maxFiles = 3,
            listChildren = { folder ->
                if (folder.id == "root") {
                    FolderListing.Success(subfolders = listOf(f1), files = listOf("1.jpg", "2.jpg", "3.jpg"))
                } else {
                    FolderListing.Success(subfolders = emptyList(), files = listOf("4.jpg"))
                }
            }
        )

        assertTrue(result.truncated)
        assertEquals(3, result.items.size)
    }

    @Test
    fun exactFileLimitReached_withoutRemainingSubfolders_isNotTruncated() {
        val root = TreeFolder("root", "Root")

        val result = TreeWalk.walk(
            rootFolder = root,
            maxFiles = 3,
            listChildren = { folder ->
                FolderListing.Success(subfolders = emptyList(), files = listOf("1.jpg", "2.jpg", "3.jpg"))
            }
        )

        assertFalse(result.truncated)
        assertEquals(3, result.items.size)
    }

    @Test
    fun unreadableSubfolders_areCountedAndNotSilentlySkipped() {
        val root = TreeFolder("root", "Root")
        val goodSub = TreeFolder("good", "Good Subfolder")
        val badSub1 = TreeFolder("bad1", "Bad Subfolder 1")
        val badSub2 = TreeFolder("bad2", "Bad Subfolder 2")

        val result = TreeWalk.walk(
            rootFolder = root,
            listChildren = { folder ->
                when (folder.id) {
                    "root" -> FolderListing.Success(
                        subfolders = listOf(badSub1, goodSub, badSub2),
                        files = listOf("root.jpg")
                    )
                    "bad1" -> FolderListing.Unreadable("Permission denied")
                    "bad2" -> throw java.io.IOException("Device I/O error")
                    "good" -> FolderListing.Success(files = listOf("good.jpg"))
                    else -> FolderListing.Success()
                }
            }
        )

        assertFalse(result.truncated)
        assertEquals(4, result.foldersVisited)
        assertEquals(2, result.unreadableSubfolders)
        assertEquals(listOf("root.jpg", "good.jpg"), result.items)
    }

    @Test
    fun cancellation_stopsImmediatelyAndMarksCancelled() {
        val root = TreeFolder("root", "Root")
        val children = (1..10).map { TreeFolder("sub$it", "Sub $it") }

        var cancelRequested = false
        var visits = 0

        val result = TreeWalk.walk(
            rootFolder = root,
            isCancelled = { cancelRequested },
            listChildren = { folder ->
                visits++
                if (visits == 2) {
                    cancelRequested = true
                }
                if (folder.id == "root") {
                    FolderListing.Success(subfolders = children, files = listOf("root.jpg"))
                } else {
                    FolderListing.Success(files = listOf("${folder.id}.jpg"))
                }
            }
        )

        assertTrue(result.wasCancelled)
        assertTrue(visits <= 3)
    }
}
