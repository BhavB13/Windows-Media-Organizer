package com.bhavb13.dtm.engine;

/**
 * One file found under the source tree, with the size/mtime snapshot taken at scan time. The
 * transfer re-reads that metadata before copying so a file edited between review and execution is
 * reported rather than silently copied in a half-written state.
 */
public final class SourceFile {

    private final String documentId;
    private final String name;
    private final String mimeType;
    /** Path relative to the source root, using '/' separators, including the file name. */
    private final String relativePath;
    /** Relative directory of the file, "" when it sits directly in the source root. */
    private final String relativeDir;
    private final long size;
    private final long lastModified;

    public SourceFile(String documentId, String name, String mimeType, String relativeDir,
                      long size, long lastModified) {
        this.documentId = documentId;
        this.name = name;
        this.mimeType = mimeType;
        this.relativeDir = relativeDir == null ? "" : relativeDir;
        this.relativePath = this.relativeDir.isEmpty() ? name : this.relativeDir + "/" + name;
        this.size = size;
        this.lastModified = lastModified;
    }

    public String documentId() {
        return documentId;
    }

    public String name() {
        return name;
    }

    public String mimeType() {
        return mimeType;
    }

    public String relativePath() {
        return relativePath;
    }

    public String relativeDir() {
        return relativeDir;
    }

    public long size() {
        return size;
    }

    public long lastModified() {
        return lastModified;
    }

    @Override
    public String toString() {
        return relativePath;
    }
}
