package com.bhavb13.dtm.engine;

/** Outcome for a single source file. */
public final class ItemResult {

    public enum Status {
        /** Copied and verified; the bytes now exist at the destination. */
        COPIED,
        /** Identical content already existed at the destination, or earlier in this batch. */
        SKIPPED_DUPLICATE,
        /** Not attempted or not finished because the run was cancelled. */
        CANCELLED,
        /** Something went wrong; {@link #message()} explains what. Nothing was published. */
        FAILED
    }

    private final String relativePath;
    private final Status status;
    private final String message;
    private final String writtenName;
    private final long bytes;

    public ItemResult(String relativePath, Status status, String message, String writtenName,
                      long bytes) {
        this.relativePath = relativePath;
        this.status = status;
        this.message = message;
        this.writtenName = writtenName;
        this.bytes = bytes;
    }

    public String relativePath() {
        return relativePath;
    }

    public Status status() {
        return status;
    }

    /** Human-readable detail for failures and skips; null when there is nothing to add. */
    public String message() {
        return message;
    }

    /** Final display name at the destination, which may differ after a collision rename. */
    public String writtenName() {
        return writtenName;
    }

    public long bytes() {
        return bytes;
    }

    @Override
    public String toString() {
        return status + " " + relativePath + (message == null ? "" : " (" + message + ")");
    }
}
