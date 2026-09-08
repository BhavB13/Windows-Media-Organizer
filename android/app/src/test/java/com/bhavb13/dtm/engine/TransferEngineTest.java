package com.bhavb13.dtm.engine;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import org.junit.Before;
import org.junit.Test;

import java.util.List;

/**
 * Behavioural tests for the transfer engine, running on a plain JVM against an in-memory store.
 *
 * <p>These cover the promises the app makes to the user: identical content is never copied twice,
 * different content is never lost to a name clash, folders are preserved, the phone is never
 * touched, and anything the engine cannot verify is reported rather than counted as a success.
 */
public class TransferEngineTest {

    private FakeDocumentStore store;
    private TransferEngine engine;
    private String source;
    private String destination;

    @Before
    public void setUp() {
        store = new FakeDocumentStore();
        engine = new TransferEngine(store);
        source = store.makeRoot("phone");
        destination = store.makeRoot("usb");
    }

    private TransferResult transfer() throws TransferAbortedException {
        return engine.transfer(source, destination, ProgressListener.NONE, Cancellation.NONE);
    }

    // ------------------------------------------------- content-based dedup

    @Test
    public void copiesEveryFileIntoAnEmptyDestination() throws Exception {
        store.makeFile(source, "a.txt", "alpha");
        store.makeFile(source, "b.txt", "beta");

        TransferResult result = transfer();

        assertEquals(2, result.copied());
        assertEquals(0, result.failed());
        assertEquals(0, result.skippedDuplicates());
        assertEquals(List.of("a.txt", "b.txt"), store.pathsUnder(destination));
        assertEquals("alpha", store.contentOf(destination, "a.txt"));
    }

    @Test
    public void skipsFileWhoseContentAlreadyExistsUnderADifferentName() throws Exception {
        store.makeFile(source, "holiday.jpg", "same-bytes");
        store.makeFile(destination, "IMG_0001.jpg", "same-bytes");

        TransferResult result = transfer();

        assertEquals(0, result.copied());
        assertEquals(1, result.skippedDuplicates());
        assertEquals("Nothing new should have been written",
                List.of("IMG_0001.jpg"), store.pathsUnder(destination));
    }

    @Test
    public void findsDuplicatesNestedDeepInTheDestination() throws Exception {
        store.makeFile(source, "clip.mp4", "movie");
        String archive = store.makeDir(destination, "archive");
        String year = store.makeDir(archive, "2019");
        store.makeFile(year, "old-name.mp4", "movie");

        TransferResult result = transfer();

        assertEquals(0, result.copied());
        assertEquals(1, result.skippedDuplicates());
        assertEquals(1, store.fileCountUnder(destination));
    }

    @Test
    public void copiesIdenticalContentOnlyOnceWithinASingleBatch() throws Exception {
        store.makeFile(source, "one.txt", "identical");
        String nested = store.makeDir(source, "copy");
        store.makeFile(nested, "two.txt", "identical");

        TransferResult result = transfer();

        assertEquals(1, result.copied());
        assertEquals(1, result.skippedDuplicates());
        assertEquals(1, store.fileCountUnder(destination));
    }

    // --------------------------------------------- same name, different bytes

    @Test
    public void keepsBothFilesWhenNamesClashButContentDiffers() throws Exception {
        store.makeFile(source, "notes.txt", "from the phone");
        store.makeFile(destination, "notes.txt", "already on the drive");

        TransferResult result = transfer();

        assertEquals(1, result.copied());
        assertEquals(0, result.skippedDuplicates());
        assertEquals(List.of("notes (1).txt", "notes.txt"), store.pathsUnder(destination));
        assertEquals("The existing file must not be touched",
                "already on the drive", store.contentOf(destination, "notes.txt"));
        assertEquals("from the phone", store.contentOf(destination, "notes (1).txt"));
    }

    @Test
    public void keepsSteppingThroughNamesUntilAFreeOneIsFound() throws Exception {
        store.makeFile(source, "notes.txt", "third version");
        store.makeFile(destination, "notes.txt", "first version");
        store.makeFile(destination, "notes (1).txt", "second version");

        TransferResult result = transfer();

        assertEquals(1, result.copied());
        assertEquals("third version", store.contentOf(destination, "notes (2).txt"));
        assertEquals("first version", store.contentOf(destination, "notes.txt"));
        assertEquals("second version", store.contentOf(destination, "notes (1).txt"));
    }

    // ------------------------------------------------------ folder structure

    @Test
    public void preservesRelativeSubfolders() throws Exception {
        String dcim = store.makeDir(source, "DCIM");
        String camera = store.makeDir(dcim, "Camera");
        store.makeFile(camera, "IMG_1.jpg", "photo-one");
        store.makeFile(source, "root.txt", "top level");

        TransferResult result = transfer();

        assertEquals(2, result.copied());
        assertEquals(List.of("DCIM/Camera/IMG_1.jpg", "root.txt"), store.pathsUnder(destination));
        assertEquals("photo-one", store.contentOf(destination, "DCIM/Camera/IMG_1.jpg"));
    }

    @Test
    public void reusesAnExistingDestinationSubfolderInsteadOfDuplicatingIt() throws Exception {
        String dcim = store.makeDir(source, "DCIM");
        store.makeFile(dcim, "new.jpg", "brand new");
        String existing = store.makeDir(destination, "DCIM");
        store.makeFile(existing, "old.jpg", "already here");

        transfer();

        assertEquals(List.of("DCIM/new.jpg", "DCIM/old.jpg"), store.pathsUnder(destination));
    }

    // ------------------------------------------------------- source is sacred

    @Test
    public void neverModifiesTheSource() throws Exception {
        store.makeFile(source, "a.txt", "alpha");
        String nested = store.makeDir(source, "sub");
        store.makeFile(nested, "b.txt", "beta");
        store.makeFile(destination, "dup.txt", "alpha");

        transfer();

        assertEquals(List.of("a.txt", "sub/b.txt"), store.pathsUnder(source));
        assertEquals("alpha", store.contentOf(source, "a.txt"));
        assertEquals("beta", store.contentOf(source, "sub/b.txt"));
    }

    // ------------------------------------------------------------ disjointness

    @Test
    public void rejectsIdenticalRoots() {
        try {
            engine.transfer(source, source, ProgressListener.NONE, Cancellation.NONE);
            fail("expected the run to be refused");
        } catch (TransferAbortedException e) {
            assertTrue(e.getMessage().contains("same folder"));
        }
    }

    @Test
    public void rejectsDestinationInsideSource() throws Exception {
        String inner = store.makeDir(source, "backup");
        store.makeFile(source, "a.txt", "alpha");
        try {
            engine.transfer(source, inner, ProgressListener.NONE, Cancellation.NONE);
            fail("expected the run to be refused");
        } catch (TransferAbortedException e) {
            assertTrue(e.getMessage().contains("inside the source"));
        }
        assertEquals("Nothing may be written when the roots overlap",
                0, store.fileCountUnder(inner));
    }

    @Test
    public void rejectsSourceInsideDestination() {
        String inner = store.makeDir(destination, "phone-backup");
        try {
            engine.transfer(inner, destination, ProgressListener.NONE, Cancellation.NONE);
            fail("expected the run to be refused");
        } catch (TransferAbortedException e) {
            assertTrue(e.getMessage().contains("inside the destination"));
        }
    }

    // ---------------------------------------------------- refusing to guess

    @Test
    public void abortsWhenTheDestinationCannotBeFullyListed() {
        store.makeFile(source, "a.txt", "alpha");
        String folder = store.makeDir(destination, "unreadable");
        store.unlistableIds.add(folder);

        try {
            transfer();
            fail("expected the run to abort");
        } catch (TransferAbortedException e) {
            assertTrue(e.getMessage().contains("could not be fully read"));
        }
        assertEquals("An unreadable drive must not receive a partial copy",
                0, store.fileCountUnder(destination));
    }

    @Test
    public void abortsWhenAnExistingDestinationFileCannotBeHashed() {
        store.makeFile(source, "a.txt", "alpha");
        String existing = store.makeFile(destination, "mystery.bin", "unknown");
        store.unreadableIds.add(existing);

        try {
            transfer();
            fail("expected the run to abort");
        } catch (TransferAbortedException e) {
            assertTrue(e.getMessage().contains("duplicates cannot be detected"));
        }
        assertEquals(1, store.fileCountUnder(destination));
    }

    @Test
    public void abortsWhenTheSourceCannotBeFullyListed() {
        String folder = store.makeDir(source, "locked");
        store.unlistableIds.add(folder);

        try {
            transfer();
            fail("expected the run to abort");
        } catch (TransferAbortedException e) {
            assertTrue(e.getMessage().contains("could not be fully read"));
        }
    }

    // ------------------------------------------------------- per-file failures

    @Test
    public void reportsUnreadableSourceFileAndStillCopiesTheRest() throws Exception {
        String broken = store.makeFile(source, "broken.txt", "unreachable");
        store.makeFile(source, "fine.txt", "readable");
        store.unreadableIds.add(broken);

        TransferResult result = transfer();

        assertEquals(1, result.copied());
        assertEquals(1, result.failed());
        // The source is hashed before anything is created on the drive, so an unreadable file
        // fails during the read and never costs a document. Only the good file is on the drive,
        // and there is no leftover partial to clean up.
        assertEquals(List.of("fine.txt"), store.pathsUnder(destination));
        assertTrue(result.failures().get(0).message().contains("Could not read this file"));
    }

    @Test
    public void doesNotCountACopyAsSuccessfulWhenTheWrittenBytesDiffer() throws Exception {
        store.makeFile(source, "photo.jpg", "the real bytes");
        // The drive accepts the bytes but stores something else.
        store.corruptEveryWrite = true;

        TransferResult result = transfer();

        assertEquals(0, result.copied());
        assertEquals(1, result.failed());
        assertTrue(result.failures().get(0).message().contains("did not match"));
        assertEquals("A file that failed verification must not be left behind",
                0, store.fileCountUnder(destination));
    }

    @Test
    public void reportsAFileThatChangesOnThePhoneMidRun() throws Exception {
        String moving = store.makeFile(source, "recording.mp4", "short");
        store.makeFile(source, "stable.txt", "steady");
        // The file grows between being listed and being copied.
        store.onNodeQuery.put(moving, () -> store.setContent(moving, "much longer content"));

        TransferResult result = transfer();

        assertEquals(1, result.copied());
        assertEquals(1, result.failed());
        assertTrue(result.failures().get(0).message().contains("changed on the phone"));
        assertEquals(List.of("stable.txt"), store.pathsUnder(destination));
    }

    @Test
    public void reportsAFailureWhenTheDestinationRefusesToCreateTheFile() throws Exception {
        store.makeFile(source, "a.txt", "alpha");
        store.uncreatableParents.add(destination);

        TransferResult result = transfer();

        assertEquals(0, result.copied());
        assertEquals(1, result.failed());
        assertTrue(result.failures().get(0).message().contains("Could not start writing"));
    }

    // ------------------------------------------------------------ cancellation

    @Test
    public void cancellationStopsEarlyAndLeavesNoPartialFiles() throws Exception {
        for (int i = 0; i < 5; i++) {
            store.makeFile(source, "file-" + i + ".txt", "content-" + i);
        }
        Cancellation.Flag flag = new Cancellation.Flag();
        ProgressListener stopAfterFirst = new ProgressListener() {
            @Override
            public void onPhase(String phase) {
            }

            @Override
            public void onScanProgress(String phase, int itemsSeen) {
            }

            @Override
            public void onFileProgress(int filesDone, int filesTotal, long bytesDone,
                                       long bytesTotal, String currentName) {
            }

            @Override
            public void onItemResult(ItemResult result) {
                flag.cancel();
            }
        };

        TransferResult result = engine.transfer(source, destination, stopAfterFirst, flag);

        assertTrue(result.wasCancelled());
        assertEquals(1, result.copied());
        assertEquals(4, result.cancelledItems());
        assertEquals(1, store.fileCountUnder(destination));
        for (String name : store.namesUnder(destination)) {
            assertFalse("A cancelled run must not leave partials behind",
                    name.endsWith(Names.PARTIAL_SUFFIX));
        }
    }

    @Test
    public void aCancelledRunCanBeRepeatedAndFinishesTheRemainingFiles() throws Exception {
        for (int i = 0; i < 3; i++) {
            store.makeFile(source, "file-" + i + ".txt", "content-" + i);
        }
        Cancellation.Flag flag = new Cancellation.Flag();
        engine.transfer(source, destination, new StopAfter(1, flag), flag);
        assertEquals(1, store.fileCountUnder(destination));

        TransferResult second = transfer();

        assertEquals("The already-copied file is recognised by content, not by name",
                1, second.skippedDuplicates());
        assertEquals(2, second.copied());
        assertEquals(3, store.fileCountUnder(destination));
    }

    /** Cancels after a fixed number of item results. */
    private static final class StopAfter implements ProgressListener {
        private final int limit;
        private final Cancellation.Flag flag;
        private int seen;

        StopAfter(int limit, Cancellation.Flag flag) {
            this.limit = limit;
            this.flag = flag;
        }

        @Override
        public void onPhase(String phase) {
        }

        @Override
        public void onScanProgress(String phase, int itemsSeen) {
        }

        @Override
        public void onFileProgress(int filesDone, int filesTotal, long bytesDone, long bytesTotal,
                                   String currentName) {
        }

        @Override
        public void onItemResult(ItemResult result) {
            if (++seen >= limit) {
                flag.cancel();
            }
        }
    }

    // ------------------------------------------------------------ repeatability

    @Test
    public void rerunningCopiesOnlyWhatIsGenuinelyNew() throws Exception {
        store.makeFile(source, "a.txt", "alpha");
        assertEquals(1, transfer().copied());

        store.makeFile(source, "b.txt", "beta");
        TransferResult second = transfer();

        assertEquals(1, second.copied());
        assertEquals(1, second.skippedDuplicates());
        assertEquals(List.of("a.txt", "b.txt"), store.pathsUnder(destination));
    }

    @Test
    public void reflectsAnEditedSourceFileOnTheNextRunWithNoStaleCache() throws Exception {
        String file = store.makeFile(source, "a.txt", "first draft");
        transfer();

        store.setContent(file, "second draft");
        TransferResult second = transfer();

        assertEquals("Changed content is new content", 1, second.copied());
        assertEquals(List.of("a (1).txt", "a.txt"), store.pathsUnder(destination));
    }

    @Test
    public void ignoresLeftoverPartialsWhenDecidingWhatIsAlreadyThere() throws Exception {
        store.makeFile(source, "a.txt", "alpha");
        store.makeFile(destination, "a.txt" + Names.PARTIAL_SUFFIX, "alpha");

        TransferResult result = transfer();

        assertEquals("A stale partial is not real content and must not suppress the copy",
                1, result.copied());
        assertNotNull(store.contentOf(destination, "a.txt"));
    }

    // -------------------------------------------------------------- preview

    @Test
    public void previewReportsWhatWouldHappenWithoutWritingAnything() throws Exception {
        store.makeFile(source, "new.txt", "fresh");
        store.makeFile(source, "dup.txt", "shared");
        store.makeFile(destination, "existing.txt", "shared");

        ScanResult scan = engine.scan(source, destination, ProgressListener.NONE,
                Cancellation.NONE);

        assertEquals(2, scan.sourceFileCount());
        assertEquals(1, scan.destinationFileCount());
        assertEquals(1, scan.newFiles().size());
        assertEquals("new.txt", scan.newFiles().get(0).relativePath());
        assertEquals(1, scan.duplicateFiles().size());
        assertEquals("A review must not write to the drive",
                1, store.fileCountUnder(destination));
    }

    @Test
    public void previewReportsUnreadableSourceFilesInsteadOfPlanningThem() throws Exception {
        String broken = store.makeFile(source, "broken.txt", "unreachable");
        store.unreadableIds.add(broken);
        store.makeFile(source, "fine.txt", "readable");

        ScanResult scan = engine.scan(source, destination, ProgressListener.NONE,
                Cancellation.NONE);

        assertEquals(1, scan.newFiles().size());
        assertEquals(1, scan.scanFailures().size());
        assertEquals("broken.txt", scan.scanFailures().get(0).relativePath());
    }

    @Test
    public void previewAbortsWhenTheDestinationIsUnreadable() {
        store.makeFile(source, "a.txt", "alpha");
        String folder = store.makeDir(destination, "unreadable");
        store.unlistableIds.add(folder);

        try {
            engine.scan(source, destination, ProgressListener.NONE, Cancellation.NONE);
            fail("expected the review to abort");
        } catch (TransferAbortedException e) {
            assertTrue(e.getMessage().contains("could not be fully read"));
        }
    }

    @Test
    public void handlesEmptyFilesAsRealContent() throws Exception {
        store.makeFile(source, "empty-a.txt", "");
        store.makeFile(source, "empty-b.txt", "");

        TransferResult result = transfer();

        assertEquals("Two empty files have identical content", 1, result.copied());
        assertEquals(1, result.skippedDuplicates());
    }
}
