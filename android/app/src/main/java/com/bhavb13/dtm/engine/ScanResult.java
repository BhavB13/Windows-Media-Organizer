package com.bhavb13.dtm.engine;

import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * What a preview scan found. Every scan is computed from scratch: there is no persisted hash cache,
 * so re-running after changing either side always reflects what is actually on the volumes now.
 */
public final class ScanResult {

    private final List<SourceFile> newFiles;
    private final List<SourceFile> duplicateFiles;
    private final List<ItemResult> scanFailures;
    private final Set<String> destinationHashes;
    private final int destinationFileCount;
    private final int sourceFileCount;
    private final long newBytes;
    private final boolean cancelled;
    private final int leftoverPartials;

    /**
     * A review that was stopped before either side had been fully read.
     *
     * <p>Every count is zero and {@link #wasCancelled()} is true, so no caller can mistake an
     * abandoned walk for a drive that turned out to be empty.
     */
    public static ScanResult cancelled() {
        return new ScanResult(new ArrayList<>(), new ArrayList<>(), new ArrayList<>(),
                new HashSet<>(), 0, 0, 0L, true, 0);
    }

    public ScanResult(List<SourceFile> newFiles, List<SourceFile> duplicateFiles,
                      List<ItemResult> scanFailures, Set<String> destinationHashes,
                      int destinationFileCount, int sourceFileCount, long newBytes,
                      boolean cancelled, int leftoverPartials) {
        this.leftoverPartials = leftoverPartials;
        this.newFiles = Collections.unmodifiableList(newFiles);
        this.duplicateFiles = Collections.unmodifiableList(duplicateFiles);
        this.scanFailures = Collections.unmodifiableList(scanFailures);
        this.destinationHashes = Collections.unmodifiableSet(destinationHashes);
        this.destinationFileCount = destinationFileCount;
        this.sourceFileCount = sourceFileCount;
        this.newBytes = newBytes;
        this.cancelled = cancelled;
    }

    /** Files whose content was not found anywhere in the destination, deduplicated within source. */
    public List<SourceFile> newFiles() {
        return newFiles;
    }

    /** Source files whose exact content already exists at the destination or earlier in the batch. */
    public List<SourceFile> duplicateFiles() {
        return duplicateFiles;
    }

    /** Source files that could not be read during the preview; they are not planned for copying. */
    public List<ItemResult> scanFailures() {
        return scanFailures;
    }

    /**
     * Full-content digests found at the destination. Used as a starting point at execution time,
     * where it is re-verified against a fresh destination scan.
     */
    public Set<String> destinationHashes() {
        return destinationHashes;
    }

    public int destinationFileCount() {
        return destinationFileCount;
    }

    public int sourceFileCount() {
        return sourceFileCount;
    }

    public long newBytes() {
        return newBytes;
    }

    public boolean wasCancelled() {
        return cancelled;
    }

    /**
     * Files already at the destination whose name carries the ".dtmpart" suffix.
     *
     * <p>They are almost certainly debris from a run that was killed, but the app cannot prove
     * ownership from a name alone, so it never deletes them. They are also kept out of the
     * duplicate set, so they can never suppress a copy; they are reported here instead, for the
     * user to decide about.
     */
    public int leftoverPartials() {
        return leftoverPartials;
    }
}
