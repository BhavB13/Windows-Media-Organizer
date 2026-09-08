package com.bhavb13.dtm.engine;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.security.MessageDigest;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * Copies every file under a source tree into a destination tree, skipping anything whose exact
 * bytes already exist at the destination.
 *
 * <p>Design rules this class enforces:
 * <ul>
 *   <li><b>Content, not names, decides duplicates.</b> Identity is the SHA-256 of the whole file.
 *       Two files with the same name but different content both survive, the second under a
 *       generated name.</li>
 *   <li><b>Decide before writing.</b> A source file is hashed <em>first</em>; a duplicate is
 *       skipped without creating any document on the drive. That is what lets a re-run against a
 *       completely full drive still succeed instead of failing on every file.</li>
 *   <li><b>The source is never modified.</b> Nothing here deletes, renames, or writes on the
 *       source side.</li>
 *   <li><b>Never overwrite.</b> Bytes always land in a freshly created ".dtmpart" document, are
 *       verified by re-reading them, and only then are renamed into place. The only documents this
 *       class deletes are partials it created during this run, tracked by id rather than
 *       recognised by their name.</li>
 *   <li><b>Refuse to guess.</b> If the destination cannot be fully enumerated, the run aborts: an
 *       unreadable destination must never be mistaken for an empty one. A cancelled enumeration is
 *       reported as cancelled, never as a complete empty answer.</li>
 *   <li><b>No cache.</b> Every run re-scans and re-hashes both sides, so a rerun after edits is
 *       correct rather than fast.</li>
 * </ul>
 */
public final class TransferEngine {

    /** Directory nesting we are willing to walk; guards against pathological provider loops. */
    private static final int MAX_DEPTH = 64;
    private static final int BUFFER = 128 * 1024;
    /** Upper bound on collision-rename attempts before a file is reported as failed. */
    private static final int MAX_RENAME_ATTEMPTS = 500;

    private final DocumentStore store;

    public TransferEngine(DocumentStore store) {
        if (store == null) {
            throw new IllegalArgumentException("store");
        }
        this.store = store;
    }

    /**
     * Thrown internally when the user cancelled part-way through a traversal.
     *
     * <p>This exists so a half-finished walk can never be returned as if it were a complete one. A
     * cancelled destination scan that came back as "no files here" would make every source file
     * look new and get re-copied.
     */
    private static final class CancelledException extends Exception {
        CancelledException() {
            super("cancelled");
        }
    }

    /** What a destination walk found. */
    private static final class DestinationScan {
        final Set<String> hashes = new HashSet<>();
        int files;
        /** Files whose name carries the partial suffix but which this run did not create. */
        int leftoverPartials;
    }

    // ---------------------------------------------------------------- preview

    /**
     * Reads both trees and reports what a transfer would do, without writing anything.
     *
     * <p>The result is for review only. {@link #transfer} repeats the whole scan itself, so a stale
     * preview can never cause a wrong copy.
     */
    public ScanResult scan(String sourceRootId, String destinationRootId,
                           ProgressListener listener, Cancellation cancellation)
            throws TransferAbortedException {
        ProgressListener out = listener == null ? ProgressListener.NONE : listener;
        Cancellation cancel = cancellation == null ? Cancellation.NONE : cancellation;
        requireDisjointRoots(sourceRootId, destinationRootId);

        DestinationScan destination;
        List<SourceFile> sources;
        try {
            out.onPhase("Reading the USB drive");
            destination = hashDestination(destinationRootId, out, cancel);

            out.onPhase("Reading the phone folder");
            sources = listSource(sourceRootId, out, cancel);
        } catch (CancelledException e) {
            // Neither side was fully read, so there is nothing trustworthy to report.
            return ScanResult.cancelled();
        }

        out.onPhase("Comparing contents");
        List<SourceFile> newFiles = new ArrayList<>();
        List<SourceFile> duplicates = new ArrayList<>();
        List<ItemResult> failures = new ArrayList<>();
        Set<String> batchHashes = new HashSet<>();
        long newBytes = 0L;
        boolean cancelled = false;
        int done = 0;

        for (SourceFile file : sources) {
            if (cancel.isCancelled()) {
                cancelled = true;
                break;
            }
            String hash;
            try {
                hash = hashDocument(file.documentId(), cancel);
            } catch (StoreException | IOException e) {
                failures.add(new ItemResult(file.relativePath(), ItemResult.Status.FAILED,
                        "Could not read this file: " + describe(e), null, 0L));
                continue;
            }
            if (hash == null) {
                cancelled = true;
                break;
            }
            done++;
            out.onFileProgress(done, sources.size(), 0L, 0L, file.relativePath());
            if (destination.hashes.contains(hash) || !batchHashes.add(hash)) {
                duplicates.add(file);
            } else {
                newFiles.add(file);
                newBytes += Math.max(0L, file.size());
            }
        }

        return new ScanResult(newFiles, duplicates, failures, destination.hashes,
                destination.files, sources.size(), newBytes, cancelled,
                destination.leftoverPartials);
    }

    // --------------------------------------------------------------- transfer

    /**
     * Performs the copy. Both trees are re-read from scratch first, so this is safe to call some
     * time after {@link #scan} and safe to re-run after a partial or cancelled attempt.
     */
    public TransferResult transfer(String sourceRootId, String destinationRootId,
                                   ProgressListener listener, Cancellation cancellation)
            throws TransferAbortedException {
        ProgressListener out = listener == null ? ProgressListener.NONE : listener;
        Cancellation cancel = cancellation == null ? Cancellation.NONE : cancellation;
        requireDisjointRoots(sourceRootId, destinationRootId);

        Set<String> knownHashes;
        List<SourceFile> sources;
        try {
            out.onPhase("Re-checking the USB drive");
            knownHashes = hashDestination(destinationRootId, out, cancel).hashes;

            out.onPhase("Re-checking the phone folder");
            sources = listSource(sourceRootId, out, cancel);
        } catch (CancelledException e) {
            // Cancelled during the re-check: the scans are incomplete, so nothing may be copied.
            return new TransferResult(new ArrayList<>(), 0L, true);
        }

        if (cancel.isCancelled()) {
            return new TransferResult(new ArrayList<>(), 0L, true);
        }

        out.onPhase("Copying");
        List<ItemResult> results = new ArrayList<>();
        long bytesTotal = 0L;
        for (SourceFile file : sources) {
            bytesTotal += Math.max(0L, file.size());
        }
        long bytesDone = 0L;
        boolean cancelled = false;
        int index = 0;
        // Ids of the partials this run created. Deleting is authorised by membership here, never
        // by a file's name: a pre-existing file that merely looks like a partial is not ours.
        Set<String> ownedPartials = new LinkedHashSet<>();

        for (SourceFile file : sources) {
            index++;
            if (cancel.isCancelled()) {
                cancelled = true;
                results.add(new ItemResult(file.relativePath(), ItemResult.Status.CANCELLED,
                        "Stopped before this file was copied", null, 0L));
                continue;
            }
            out.onFileProgress(index - 1, sources.size(), bytesDone, bytesTotal,
                    file.relativePath());
            ItemResult result = copyOne(file, destinationRootId, knownHashes, ownedPartials, cancel);
            results.add(result);
            out.onItemResult(result);
            if (result.status() == ItemResult.Status.COPIED) {
                bytesDone += result.bytes();
            }
            if (result.status() == ItemResult.Status.CANCELLED) {
                cancelled = true;
            }
            out.onFileProgress(index, sources.size(), bytesDone, bytesTotal, file.relativePath());
        }

        return new TransferResult(results, bytesDone, cancelled);
    }

    /**
     * Copies one file.
     *
     * <p>Order matters here. The source is hashed before anything is created on the drive, so a
     * duplicate costs no space and no write — the case that decides whether a re-run against an
     * almost-full drive works at all. Only once the content is known to be new are bytes streamed
     * into a fresh partial, re-read, and published with a rename. Every early exit removes the
     * partial, and only the partial.
     */
    private ItemResult copyOne(SourceFile file, String destinationRootId, Set<String> knownHashes,
                               Set<String> ownedPartials, Cancellation cancel) {
        String path = file.relativePath();

        // The source may have changed since it was listed; a file being written right now would
        // otherwise be copied truncated and then recorded as a successful duplicate-free copy.
        DocumentNode before;
        try {
            before = store.node(file.documentId());
        } catch (StoreException e) {
            return failure(path, "This file is no longer available: " + describe(e));
        }
        if (before.isDirectory()) {
            return failure(path, "This item turned into a folder while the app was working");
        }
        if (changedSince(file, before)) {
            return failure(path, "The file changed on the phone during the copy; skipped for safety");
        }

        // Read one: establish what this file's content actually is, before touching the drive.
        String sourceHash;
        try {
            sourceHash = hashDocument(file.documentId(), cancel);
        } catch (StoreException | IOException e) {
            return failure(path, "Could not read this file: " + describe(e));
        }
        if (sourceHash == null) {
            return new ItemResult(path, ItemResult.Status.CANCELLED,
                    "Stopped before this file was read", null, 0L);
        }

        // The duplicate decision happens here: nothing has been created on the drive yet.
        if (knownHashes.contains(sourceHash)) {
            return new ItemResult(path, ItemResult.Status.SKIPPED_DUPLICATE,
                    "Identical content is already on the drive", null, 0L);
        }

        // The hash is only a usable fingerprint if the file held still while it was read.
        DocumentNode afterHash;
        try {
            afterHash = store.node(file.documentId());
        } catch (StoreException e) {
            return failure(path, "This file is no longer available: " + describe(e));
        }
        if (changedSince(file, afterHash)) {
            return failure(path, "The file changed on the phone during the copy; skipped for safety");
        }

        String parentId;
        try {
            parentId = ensureDirectory(destinationRootId, file.relativeDir());
        } catch (StoreException e) {
            return failure(path, "Could not create the folder on the drive: " + describe(e));
        }

        String partialId;
        try {
            partialId = store.createUniqueDocument(parentId, file.mimeType(),
                    file.name() + Names.PARTIAL_SUFFIX);
        } catch (StoreException e) {
            return failure(path, "Could not start writing to the drive: " + describe(e));
        }
        ownedPartials.add(partialId);

        // Read two: the bytes actually handed to the drive, digested on the way past.
        String streamedHash;
        long written;
        try {
            long[] counter = new long[1];
            streamedHash = streamAndHash(file.documentId(), partialId, counter, cancel);
            written = counter[0];
        } catch (StoreException | IOException e) {
            return discardAndFail(path, partialId, ownedPartials, "Copy failed: " + describe(e));
        }
        if (streamedHash == null) {
            return discardAndCancel(path, partialId, ownedPartials,
                    "Stopped part-way; the incomplete file was removed");
        }
        if (!streamedHash.equals(sourceHash)) {
            // The file was rewritten between the two reads, so neither digest describes a settled
            // file. Copying either one would record a snapshot that never existed as a whole.
            return discardAndFail(path, partialId, ownedPartials,
                    "The file changed on the phone during the copy; skipped for safety");
        }

        DocumentNode afterCopy;
        try {
            afterCopy = store.node(file.documentId());
        } catch (StoreException e) {
            return discardAndFail(path, partialId, ownedPartials,
                    "This file is no longer available: " + describe(e));
        }
        if (changedSince(file, afterCopy)) {
            return discardAndFail(path, partialId, ownedPartials,
                    "The file changed on the phone during the copy; skipped for safety");
        }

        // Read the written document back; a copy is only a success if the drive really holds it.
        String verifyHash;
        try {
            verifyHash = hashDocument(partialId, cancel);
        } catch (StoreException | IOException e) {
            return discardAndFail(path, partialId, ownedPartials,
                    "Could not verify the copied file: " + describe(e));
        }
        if (verifyHash == null) {
            return discardAndCancel(path, partialId, ownedPartials,
                    "Stopped before this file could be verified");
        }
        if (!verifyHash.equals(sourceHash)) {
            return discardAndFail(path, partialId, ownedPartials,
                    "The copy on the drive did not match the original; nothing kept");
        }

        String publishedName;
        try {
            publishedName = publish(partialId, parentId, file.name());
        } catch (StoreException e) {
            return discardAndFail(path, partialId, ownedPartials,
                    "Could not finish saving the file: " + describe(e));
        }

        ownedPartials.remove(partialId);
        knownHashes.add(sourceHash);
        return new ItemResult(path, ItemResult.Status.COPIED, null, publishedName, written);
    }

    /**
     * Renames the partial to its final name, stepping to "name (1)", "name (2)", ... when a
     * different file already holds the name. Renaming never replaces an existing document: the
     * store contract makes a colliding rename throw, and we re-check for the name first.
     */
    private String publish(String partialId, String parentId, String desiredName)
            throws StoreException {
        StoreException last = null;
        for (int attempt = 0; attempt < MAX_RENAME_ATTEMPTS; attempt++) {
            String candidate = Names.candidate(desiredName, attempt);
            if (store.childByName(parentId, candidate) != null) {
                continue;
            }
            String publishedId;
            try {
                // A rename may hand back a new id, so the old one must not be reused afterwards.
                publishedId = store.rename(partialId, candidate);
            } catch (StoreException e) {
                // Lost a race, or the provider refused this exact name; try the next candidate.
                last = e;
                continue;
            }
            // Trust the store's post-rename view rather than the name we asked for: a provider is
            // allowed to hand back a different unique name, and that is safe. What is not safe is
            // a rename that silently did nothing, which would leave the file published under its
            // ".dtmpart" name and therefore invisible to the user.
            String actual = store.node(publishedId).name();
            if (actual.endsWith(Names.PARTIAL_SUFFIX)) {
                throw new StoreException(
                        "The drive reported a successful rename but the file is still named \""
                                + actual + "\".");
            }
            return actual;
        }
        throw new StoreException("Could not find a free name for \"" + desiredName + "\"", last);
    }

    // ------------------------------------------------------------- traversals

    /**
     * Hashes every file under the destination. Any listing error aborts: silently treating an
     * unreadable folder as empty would let the app re-copy files that are already there, or worse,
     * report a clean dedup that never happened.
     *
     * <p>Files are hashed regardless of their name. A document that merely ends in ".dtmpart" is
     * not assumed to be one of ours — it could be the user's own data — so it is counted as
     * destination content and reported rather than ignored or removed.
     */
    private DestinationScan hashDestination(String rootId, ProgressListener out, Cancellation cancel)
            throws TransferAbortedException, CancelledException {
        DestinationScan result = new DestinationScan();
        Deque<String[]> queue = new ArrayDeque<>();
        Set<String> seenDirs = new LinkedHashSet<>();
        queue.add(new String[]{rootId, "0"});
        seenDirs.add(rootId);

        while (!queue.isEmpty()) {
            if (cancel.isCancelled()) {
                throw new CancelledException();
            }
            String[] entry = queue.removeFirst();
            int depth = Integer.parseInt(entry[1]);
            if (depth > MAX_DEPTH) {
                throw new TransferAbortedException(
                        "The drive has folders nested deeper than this app will follow.");
            }
            List<DocumentNode> children;
            try {
                children = store.listChildren(entry[0]);
            } catch (StoreException e) {
                throw new TransferAbortedException(
                        "The USB drive could not be fully read, so the app cannot tell what is "
                                + "already on it. Nothing was copied. (" + describe(e) + ")", e);
            }
            for (DocumentNode child : children) {
                if (cancel.isCancelled()) {
                    throw new CancelledException();
                }
                if (child.isDirectory()) {
                    if (seenDirs.add(child.id())) {
                        queue.add(new String[]{child.id(), Integer.toString(depth + 1)});
                    }
                    continue;
                }
                boolean looksPartial = child.name().endsWith(Names.PARTIAL_SUFFIX);
                String hash;
                try {
                    hash = hashDocument(child.id(), cancel);
                } catch (StoreException | IOException e) {
                    throw new TransferAbortedException(
                            "A file already on the drive could not be read (\"" + child.name()
                                    + "\"), so duplicates cannot be detected reliably. Nothing was "
                                    + "copied. (" + describe(e) + ")", e);
                }
                if (hash == null) {
                    throw new CancelledException();
                }
                if (looksPartial) {
                    // A name is not proof of ownership, so this file is never deleted and never
                    // assumed to be ours. It is also kept out of the duplicate set, which is the
                    // fail-safe direction: a partial is usually an incomplete file, and letting it
                    // suppress a copy could leave the user's only complete copy of that content
                    // sitting under a ".dtmpart" name. Copying again merely costs space.
                    result.leftoverPartials++;
                    continue;
                }
                result.hashes.add(hash);
                result.files++;
                out.onScanProgress("Reading the USB drive", result.files);
            }
        }
        return result;
    }

    /** Lists every file under the source, preserving each file's relative folder path. */
    private List<SourceFile> listSource(String rootId, ProgressListener out, Cancellation cancel)
            throws TransferAbortedException, CancelledException {
        List<SourceFile> files = new ArrayList<>();
        Deque<Object[]> queue = new ArrayDeque<>();
        Set<String> seenDirs = new LinkedHashSet<>();
        queue.add(new Object[]{rootId, "", 0});
        seenDirs.add(rootId);

        while (!queue.isEmpty()) {
            if (cancel.isCancelled()) {
                throw new CancelledException();
            }
            Object[] entry = queue.removeFirst();
            String dirId = (String) entry[0];
            String relative = (String) entry[1];
            int depth = (Integer) entry[2];
            if (depth > MAX_DEPTH) {
                throw new TransferAbortedException(
                        "The selected folder is nested deeper than this app will follow.");
            }
            List<DocumentNode> children;
            try {
                children = store.listChildren(dirId);
            } catch (StoreException e) {
                throw new TransferAbortedException(
                        "The phone folder could not be fully read, so some files would be missed. "
                                + "Nothing was copied. (" + describe(e) + ")", e);
            }
            for (DocumentNode child : children) {
                if (cancel.isCancelled()) {
                    throw new CancelledException();
                }
                if (child.isDirectory()) {
                    String childRelative =
                            relative.isEmpty() ? child.name() : relative + "/" + child.name();
                    if (seenDirs.add(child.id())) {
                        queue.add(new Object[]{child.id(), childRelative, depth + 1});
                    }
                    continue;
                }
                files.add(new SourceFile(child.id(), child.name(), child.mimeType(), relative,
                        child.size(), child.lastModified()));
                out.onScanProgress("Reading the phone folder", files.size());
            }
        }
        return files;
    }

    /** Walks/creates the destination folder chain matching a source-relative directory. */
    private String ensureDirectory(String rootId, String relativeDir) throws StoreException {
        if (relativeDir == null || relativeDir.isEmpty()) {
            return rootId;
        }
        String current = rootId;
        for (String segment : relativeDir.split("/")) {
            if (segment.isEmpty()) {
                continue;
            }
            current = store.createOrOpenDirectory(current, segment);
        }
        return current;
    }

    // -------------------------------------------------------------- utilities

    /** True when the live metadata no longer matches the snapshot taken when the file was listed. */
    private static boolean changedSince(SourceFile snapshot, DocumentNode live) {
        if (snapshot.size() >= 0 && live.size() >= 0 && live.size() != snapshot.size()) {
            return true;
        }
        return snapshot.lastModified() > 0 && live.lastModified() > 0
                && live.lastModified() != snapshot.lastModified();
    }

    /** @return the digest, or null if cancelled part-way through. */
    private String hashDocument(String documentId, Cancellation cancel)
            throws StoreException, IOException {
        MessageDigest digest = Sha256.newDigest();
        byte[] buffer = new byte[BUFFER];
        try (InputStream in = store.openRead(documentId)) {
            int read;
            while ((read = in.read(buffer)) != -1) {
                if (cancel.isCancelled()) {
                    return null;
                }
                digest.update(buffer, 0, read);
            }
        }
        return Sha256.hex(digest.digest());
    }

    /**
     * Streams source bytes into the target document while digesting them, so the hash describes
     * exactly the bytes that were handed to the drive.
     *
     * @return the digest, or null if cancelled part-way through
     */
    private String streamAndHash(String sourceId, String targetId, long[] bytesOut,
                                 Cancellation cancel) throws StoreException, IOException {
        MessageDigest digest = Sha256.newDigest();
        byte[] buffer = new byte[BUFFER];
        long total = 0L;
        try (InputStream in = store.openRead(sourceId);
             OutputStream target = store.openWrite(targetId)) {
            int read;
            while ((read = in.read(buffer)) != -1) {
                if (cancel.isCancelled()) {
                    return null;
                }
                digest.update(buffer, 0, read);
                target.write(buffer, 0, read);
                total += read;
            }
            target.flush();
        }
        bytesOut[0] = total;
        return Sha256.hex(digest.digest());
    }

    /**
     * Rejects a source and destination that overlap. Copying a tree into itself, or into its own
     * parent, would keep discovering the files it just wrote.
     */
    private void requireDisjointRoots(String sourceRootId, String destinationRootId)
            throws TransferAbortedException {
        if (sourceRootId == null || destinationRootId == null) {
            throw new TransferAbortedException("Pick both a phone folder and a drive folder first.");
        }
        if (sourceRootId.equals(destinationRootId)) {
            throw new TransferAbortedException(
                    "The source and destination are the same folder. Pick two different folders.");
        }
        try {
            if (store.isSameOrDescendant(sourceRootId, destinationRootId)) {
                throw new TransferAbortedException(
                        "The destination folder is inside the source folder. Pick a destination "
                                + "outside it.");
            }
            if (store.isSameOrDescendant(destinationRootId, sourceRootId)) {
                throw new TransferAbortedException(
                        "The source folder is inside the destination folder. Pick a source outside "
                                + "it.");
            }
        } catch (StoreException e) {
            throw new TransferAbortedException(
                    "The selected folders could not be checked: " + describe(e), e);
        }
    }

    private ItemResult discardAndFail(String path, String partialId, Set<String> ownedPartials,
                                      String message) {
        return new ItemResult(path, ItemResult.Status.FAILED,
                message + discardPartial(partialId, ownedPartials), null, 0L);
    }

    private ItemResult discardAndCancel(String path, String partialId, Set<String> ownedPartials,
                                        String message) {
        return new ItemResult(path, ItemResult.Status.CANCELLED,
                message + discardPartial(partialId, ownedPartials), null, 0L);
    }

    /**
     * Deletes a partial this run created, and only such a partial.
     *
     * @return "" when the partial is gone, or a sentence to append to the item's message. A failed
     *     cleanup is never swallowed: the user needs to know a stray file was left on the drive.
     */
    private String discardPartial(String partialId, Set<String> ownedPartials) {
        if (!ownedPartials.remove(partialId)) {
            // Not ours to delete. Reaching here would be a bug, not a drive problem.
            return "";
        }
        try {
            store.delete(partialId);
            return "";
        } catch (StoreException e) {
            return " (an incomplete file could not be removed from the drive and is still there: "
                    + describe(e) + ")";
        }
    }

    private static ItemResult failure(String path, String message) {
        return new ItemResult(path, ItemResult.Status.FAILED, message, null, 0L);
    }

    private static String describe(Exception e) {
        String message = e.getMessage();
        return message == null || message.isEmpty() ? e.getClass().getSimpleName() : message;
    }
}
