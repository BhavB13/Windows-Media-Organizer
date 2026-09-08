package com.bhavb13.dtm.engine;

/**
 * Immutable metadata for one document in a {@link DocumentStore}.
 *
 * <p>{@code id} is opaque to the engine. On Android it is a document URI string; in tests it is a
 * fake-store key. The engine never parses it or assumes it is a filesystem path.
 */
public final class DocumentNode {

    private final String id;
    private final String name;
    private final String mimeType;
    private final long size;
    private final long lastModified;
    private final boolean directory;

    public DocumentNode(String id, String name, String mimeType, long size, long lastModified,
                        boolean directory) {
        if (id == null) {
            throw new IllegalArgumentException("id");
        }
        this.id = id;
        this.name = name == null ? "" : name;
        this.mimeType = mimeType == null ? "application/octet-stream" : mimeType;
        this.size = size;
        this.lastModified = lastModified;
        this.directory = directory;
    }

    public String id() {
        return id;
    }

    public String name() {
        return name;
    }

    public String mimeType() {
        return mimeType;
    }

    /** Size in bytes, or -1 when the provider does not report one. */
    public long size() {
        return size;
    }

    /** Modification time in millis, or 0 when the provider does not report one. */
    public long lastModified() {
        return lastModified;
    }

    public boolean isDirectory() {
        return directory;
    }

    @Override
    public String toString() {
        return (directory ? "dir:" : "file:") + name + "@" + id;
    }
}
