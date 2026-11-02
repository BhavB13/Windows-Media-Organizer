package com.bhavb13.dtm.engine;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * In-memory {@link DocumentStore} for the JVM tests, with hooks for the failure modes that matter:
 * unreadable folders, unreadable files, write failures, silently corrupted writes, and content
 * changing underneath a run.
 */
final class FakeDocumentStore implements DocumentStore {

    private static final class Entry {
        final String id;
        String name;
        final String mime;
        final boolean directory;
        final String parentId;
        byte[] content;
        long lastModified;

        Entry(String id, String name, String mime, boolean directory, String parentId,
              byte[] content) {
            this.id = id;
            this.name = name;
            this.mime = mime;
            this.directory = directory;
            this.parentId = parentId;
            this.content = content;
            this.lastModified = 1_000L;
        }
    }

    private final Map<String, Entry> entries = new LinkedHashMap<>();
    private int nextId = 0;

    /** Directories whose listing throws, simulating a provider or drive failure. */
    final Set<String> unlistableIds = new HashSet<>();
    /** Documents whose read throws. */
    final Set<String> unreadableIds = new HashSet<>();
    /** Documents whose write throws. */
    final Set<String> unwritableIds = new HashSet<>();
    /** Parents that refuse document creation. */
    final Set<String> uncreatableParents = new HashSet<>();
    /** Documents whose written bytes land corrupted, to exercise read-back verification. */
    final Set<String> corruptWriteTargets = new HashSet<>();
    /** When set, every write stores something other than the bytes handed to it. */
    boolean corruptEveryWrite;
    /** Runs once the first time a document's metadata is queried; used to mutate it mid-run. */
    final Map<String, Runnable> onNodeQuery = new HashMap<>();
    /** Documents whose deletion throws, simulating a drive that refuses to remove a file. */
    final Set<String> undeletableIds = new HashSet<>();
    /** When set, no new document can be created anywhere: a completely full drive. */
    boolean driveIsFull;
    /** When set, rename reports success but leaves the name untouched. */
    boolean renameSilentlyDoesNothing;
    /** When set, every created document gets this appended to its name, whatever its type. */
    String nameSuffixOnCreate;
    /** The MIME type of every document the engine asked to have created, in order. */
    final List<String> createdMimeTypes = new ArrayList<>();
    /**
     * Runs each time a document is opened for reading, after the count below is incremented. Used
     * to change a file's content between the engine's hashing pass and its copying pass.
     */
    final Map<String, Runnable> onOpenRead = new HashMap<>();
    /** How many times each document has been opened for reading. */
    final Map<String, Integer> readCounts = new HashMap<>();

    int deleteCount;
    /** How many documents the engine asked to have created; 0 proves nothing was written. */
    int createCount;

    // ------------------------------------------------------------- test setup

    String makeRoot(String name) {
        Entry entry = new Entry(newId(), name, "vnd.android.document/directory", true, null, null);
        entries.put(entry.id, entry);
        return entry.id;
    }

    String makeDir(String parentId, String name) {
        Entry entry = new Entry(newId(), name, "vnd.android.document/directory", true, parentId,
                null);
        entries.put(entry.id, entry);
        return entry.id;
    }

    String makeFile(String parentId, String name, String content) {
        return makeFile(parentId, name, content.getBytes(StandardCharsets.UTF_8));
    }

    String makeFile(String parentId, String name, byte[] content) {
        return makeFile(parentId, name, "application/octet-stream", content);
    }

    String makeFile(String parentId, String name, String mimeType, byte[] content) {
        Entry entry = new Entry(newId(), name, mimeType, false, parentId, content);
        entries.put(entry.id, entry);
        return entry.id;
    }

    /** Every file name that exists anywhere under a root, for assertions. */
    List<String> namesUnder(String rootId) {
        List<String> names = new ArrayList<>();
        for (Entry entry : entries.values()) {
            if (!entry.directory && isUnder(rootId, entry)) {
                names.add(entry.name);
            }
        }
        java.util.Collections.sort(names);
        return names;
    }

    /** Relative paths of every file under a root, for structure assertions. */
    List<String> pathsUnder(String rootId) {
        List<String> paths = new ArrayList<>();
        for (Entry entry : entries.values()) {
            if (!entry.directory && isUnder(rootId, entry)) {
                paths.add(relativePath(rootId, entry));
            }
        }
        java.util.Collections.sort(paths);
        return paths;
    }

    String contentOf(String rootId, String relativePath) {
        for (Entry entry : entries.values()) {
            if (!entry.directory && isUnder(rootId, entry)
                    && relativePath.equals(relativePath(rootId, entry))) {
                return new String(entry.content, StandardCharsets.UTF_8);
            }
        }
        return null;
    }

    int fileCountUnder(String rootId) {
        return pathsUnder(rootId).size();
    }

    void setContent(String documentId, String content) {
        Entry entry = entries.get(documentId);
        entry.content = content.getBytes(StandardCharsets.UTF_8);
        entry.lastModified += 1;
    }

    // ---------------------------------------------------------- store contract

    @Override
    public DocumentNode node(String documentId) throws StoreException {
        Runnable hook = onNodeQuery.remove(documentId);
        if (hook != null) {
            hook.run();
        }
        Entry entry = entries.get(documentId);
        if (entry == null) {
            throw new StoreException("gone: " + documentId);
        }
        return toNode(entry);
    }

    @Override
    public List<DocumentNode> listChildren(String directoryId) throws StoreException {
        if (unlistableIds.contains(directoryId)) {
            throw StoreException.unavailable("cannot list " + directoryId, null);
        }
        Entry parent = entries.get(directoryId);
        if (parent == null || !parent.directory) {
            throw new StoreException("not a folder: " + directoryId);
        }
        List<DocumentNode> out = new ArrayList<>();
        for (Entry entry : entries.values()) {
            if (directoryId.equals(entry.parentId)) {
                out.add(toNode(entry));
            }
        }
        return out;
    }

    @Override
    public DocumentNode childByName(String directoryId, String displayName) throws StoreException {
        for (DocumentNode child : listChildren(directoryId)) {
            if (child.name().equals(displayName)) {
                return child;
            }
        }
        return null;
    }

    @Override
    public InputStream openRead(String documentId) throws StoreException {
        if (unreadableIds.contains(documentId)) {
            throw new StoreException("cannot read " + documentId);
        }
        readCounts.merge(documentId, 1, Integer::sum);
        Runnable hook = onOpenRead.get(documentId);
        if (hook != null) {
            hook.run();
        }
        Entry entry = entries.get(documentId);
        if (entry == null || entry.directory) {
            throw new StoreException("not a file: " + documentId);
        }
        return new ByteArrayInputStream(entry.content);
    }

    @Override
    public OutputStream openWrite(String documentId) throws StoreException {
        if (unwritableIds.contains(documentId)) {
            throw new StoreException("cannot write " + documentId);
        }
        final Entry entry = entries.get(documentId);
        if (entry == null || entry.directory) {
            throw new StoreException("not a file: " + documentId);
        }
        entry.content = new byte[0];
        return new ByteArrayOutputStream() {
            @Override
            public void close() throws IOException {
                super.close();
                byte[] written = toByteArray();
                if (corruptEveryWrite || corruptWriteTargets.contains(entry.id)) {
                    // A drive that accepts bytes and stores something else: exactly what the
                    // read-back verification exists to catch.
                    written = "corrupted".getBytes(StandardCharsets.UTF_8);
                }
                entry.content = written;
            }
        };
    }

    @Override
    public String createUniqueDocument(String parentId, String mimeType, String displayName)
            throws StoreException {
        createCount++;
        if (driveIsFull) {
            throw new StoreException("no space left on the drive");
        }
        if (uncreatableParents.contains(parentId)) {
            throw new StoreException("cannot create in " + parentId);
        }
        if (!entries.containsKey(parentId)) {
            throw new StoreException("no such folder: " + parentId);
        }
        createdMimeTypes.add(mimeType);
        String requested = displayName + impliedExtension(mimeType, displayName);
        if (nameSuffixOnCreate != null) {
            requested += nameSuffixOnCreate;
        }
        String name = requested;
        int suffix = 0;
        while (childByName(parentId, name) != null) {
            suffix++;
            name = Names.candidate(requested, suffix);
        }
        Entry entry = new Entry(newId(), name, mimeType, false, parentId, new byte[0]);
        entries.put(entry.id, entry);
        return entry.id;
    }

    @Override
    public String createOrOpenDirectory(String parentId, String displayName) throws StoreException {
        DocumentNode existing = childByName(parentId, displayName);
        if (existing != null) {
            if (!existing.isDirectory()) {
                throw new StoreException("a file already uses the name " + displayName);
            }
            return existing.id();
        }
        return makeDir(parentId, displayName);
    }

    @Override
    public String rename(String documentId, String displayName) throws StoreException {
        Entry entry = entries.get(documentId);
        if (entry == null) {
            throw new StoreException("gone: " + documentId);
        }
        if (childByName(entry.parentId, displayName) != null) {
            // Never silently replace: the engine must pick another name.
            throw new StoreException("name already taken: " + displayName);
        }
        if (!renameSilentlyDoesNothing) {
            entry.name = displayName;
        }
        return documentId;
    }

    @Override
    public void delete(String documentId) throws StoreException {
        if (undeletableIds.contains(documentId)) {
            throw new StoreException("the drive refused to delete " + documentId);
        }
        if (entries.remove(documentId) == null) {
            throw new StoreException("gone: " + documentId);
        }
        deleteCount++;
    }

    @Override
    public boolean isSameOrDescendant(String ancestorId, String candidateId) {
        String current = candidateId;
        while (current != null) {
            if (current.equals(ancestorId)) {
                return true;
            }
            Entry entry = entries.get(current);
            current = entry == null ? null : entry.parentId;
        }
        return false;
    }

    // -------------------------------------------------------------- internals

    private boolean isUnder(String rootId, Entry entry) {
        return isSameOrDescendant(rootId, entry.id) && !rootId.equals(entry.id);
    }

    private String relativePath(String rootId, Entry entry) {
        StringBuilder path = new StringBuilder(entry.name);
        String current = entry.parentId;
        while (current != null && !current.equals(rootId)) {
            Entry parent = entries.get(current);
            if (parent == null) {
                break;
            }
            path.insert(0, parent.name + "/");
            current = parent.parentId;
        }
        return path.toString();
    }

    private DocumentNode toNode(Entry entry) {
        return new DocumentNode(entry.id, entry.name, entry.mime,
                entry.directory ? -1L : entry.content.length, entry.lastModified, entry.directory);
    }

    /**
     * Mirrors the system external-storage provider, measured on a device: a typed document whose
     * name lacks the extension its type implies gets that extension appended, so "a.jpg.dtmpart"
     * created as image/jpeg becomes "a.jpg.dtmpart.jpg". Untyped documents keep their name.
     */
    private static String impliedExtension(String mimeType, String displayName) {
        String extension;
        switch (mimeType == null ? "" : mimeType) {
            case "image/jpeg":
                extension = ".jpg";
                break;
            case "video/mp4":
                extension = ".mp4";
                break;
            case "text/plain":
                extension = ".txt";
                break;
            default:
                return "";
        }
        return displayName.toLowerCase(java.util.Locale.ROOT).endsWith(extension) ? "" : extension;
    }

    private String newId() {
        return "doc-" + (nextId++);
    }
}
