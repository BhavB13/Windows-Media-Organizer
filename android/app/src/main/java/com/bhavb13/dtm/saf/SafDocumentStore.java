package com.bhavb13.dtm.saf;

import android.content.ContentResolver;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.provider.DocumentsContract;

import com.bhavb13.dtm.engine.DocumentNode;
import com.bhavb13.dtm.engine.DocumentStore;
import com.bhavb13.dtm.engine.StoreException;

import java.io.FileNotFoundException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.List;

/**
 * {@link DocumentStore} backed by the Storage Access Framework.
 *
 * <p>Document ids are full tree-scoped document URI strings, so nothing here assumes a filesystem
 * path. That is what lets the same engine drive an internal folder, an SD card, and a USB OTG
 * drive, none of which the app can reach through {@code java.io.File} on modern Android.
 */
public final class SafDocumentStore implements DocumentStore {

    private static final String[] COLUMNS = {
            DocumentsContract.Document.COLUMN_DOCUMENT_ID,
            DocumentsContract.Document.COLUMN_DISPLAY_NAME,
            DocumentsContract.Document.COLUMN_MIME_TYPE,
            DocumentsContract.Document.COLUMN_SIZE,
            DocumentsContract.Document.COLUMN_LAST_MODIFIED,
    };

    private final ContentResolver resolver;

    public SafDocumentStore(ContentResolver resolver) {
        this.resolver = resolver;
    }

    /** Converts a picked tree Uri into the document id the engine should use as a root. */
    public static String rootIdForTree(Uri treeUri) {
        return DocumentsContract.buildDocumentUriUsingTree(
                treeUri, DocumentsContract.getTreeDocumentId(treeUri)).toString();
    }

    @Override
    public DocumentNode node(String documentId) throws StoreException {
        Uri uri = Uri.parse(documentId);
        try (Cursor cursor = resolver.query(uri, COLUMNS, null, null, null)) {
            if (cursor == null) {
                throw StoreException.unavailable(
                        "The storage provider did not respond. The drive may have been unplugged.",
                        null);
            }
            requireComplete(cursor);
            if (!cursor.moveToFirst()) {
                throw new StoreException("This item no longer exists.");
            }
            return fromCursor(uri, cursor);
        } catch (SecurityException e) {
            throw StoreException.unavailable("Access to this folder was withdrawn.", e);
        } catch (StoreException e) {
            throw e;
        } catch (Exception e) {
            throw new StoreException("Could not read this item.", e);
        }
    }

    @Override
    public List<DocumentNode> listChildren(String directoryId) throws StoreException {
        Uri parent = Uri.parse(directoryId);
        Uri children = DocumentsContract.buildChildDocumentsUriUsingTree(
                parent, DocumentsContract.getDocumentId(parent));
        List<DocumentNode> out = new ArrayList<>();
        try (Cursor cursor = resolver.query(children, COLUMNS, null, null, null)) {
            if (cursor == null) {
                throw StoreException.unavailable(
                        "This folder could not be listed. The drive may have been unplugged.", null);
            }
            requireComplete(cursor);
            while (cursor.moveToNext()) {
                String childId = cursor.getString(0);
                Uri childUri = DocumentsContract.buildDocumentUriUsingTree(parent, childId);
                out.add(fromCursor(childUri, cursor));
            }
        } catch (SecurityException e) {
            throw StoreException.unavailable("Access to this folder was withdrawn.", e);
        } catch (StoreException e) {
            throw e;
        } catch (Exception e) {
            throw new StoreException("Could not list this folder.", e);
        }
        return out;
    }

    @Override
    public DocumentNode childByName(String directoryId, String displayName) throws StoreException {
        for (DocumentNode child : listChildren(directoryId)) {
            if (displayName.equals(child.name())) {
                return child;
            }
        }
        return null;
    }

    @Override
    public InputStream openRead(String documentId) throws StoreException {
        try {
            InputStream in = resolver.openInputStream(Uri.parse(documentId));
            if (in == null) {
                throw new StoreException("This file could not be opened.");
            }
            return in;
        } catch (FileNotFoundException e) {
            throw new StoreException("This file is no longer there.", e);
        } catch (SecurityException e) {
            throw StoreException.unavailable("Permission to read this file was withdrawn.", e);
        } catch (StoreException e) {
            throw e;
        } catch (Exception e) {
            throw new StoreException("This file could not be opened.", e);
        }
    }

    @Override
    public OutputStream openWrite(String documentId) throws StoreException {
        Uri uri = Uri.parse(documentId);
        try {
            OutputStream out;
            try {
                // "wt" truncates; harmless here because we only ever write documents we created,
                // but it keeps a retried partial from being appended to.
                out = resolver.openOutputStream(uri, "wt");
            } catch (IllegalArgumentException | UnsupportedOperationException e) {
                out = resolver.openOutputStream(uri, "w");
            }
            if (out == null) {
                throw new StoreException("This file could not be opened for writing.");
            }
            return out;
        } catch (FileNotFoundException e) {
            throw new StoreException("The destination file vanished.", e);
        } catch (SecurityException e) {
            throw StoreException.unavailable("Permission to write to the drive was withdrawn.", e);
        } catch (StoreException e) {
            throw e;
        } catch (Exception e) {
            throw new StoreException("Could not write to the drive.", e);
        }
    }

    @Override
    public String createUniqueDocument(String parentId, String mimeType, String displayName)
            throws StoreException {
        try {
            Uri created = DocumentsContract.createDocument(
                    resolver, Uri.parse(parentId), safeMime(mimeType), displayName);
            if (created == null) {
                throw new StoreException("The drive refused to create \"" + displayName + "\".");
            }
            return created.toString();
        } catch (FileNotFoundException e) {
            throw StoreException.unavailable("The destination folder is gone.", e);
        } catch (SecurityException e) {
            throw StoreException.unavailable("Permission to write to the drive was withdrawn.", e);
        } catch (StoreException e) {
            throw e;
        } catch (Exception e) {
            throw new StoreException("Could not create \"" + displayName + "\" on the drive.", e);
        }
    }

    @Override
    public String createOrOpenDirectory(String parentId, String displayName) throws StoreException {
        DocumentNode existing = childByName(parentId, displayName);
        if (existing != null) {
            if (!existing.isDirectory()) {
                throw new StoreException(
                        "\"" + displayName + "\" already exists on the drive as a file, so the "
                                + "matching folder cannot be created.");
            }
            return existing.id();
        }
        try {
            Uri created = DocumentsContract.createDocument(resolver, Uri.parse(parentId),
                    DocumentsContract.Document.MIME_TYPE_DIR, displayName);
            if (created == null) {
                throw new StoreException("Could not create the folder \"" + displayName + "\".");
            }
            return created.toString();
        } catch (FileNotFoundException e) {
            throw StoreException.unavailable("The destination folder is gone.", e);
        } catch (StoreException e) {
            throw e;
        } catch (Exception e) {
            throw new StoreException("Could not create the folder \"" + displayName + "\".", e);
        }
    }

    /**
     * Renames a document and confirms the result by reading the document back.
     *
     * <p>{@code DocumentsContract.renameDocument} carries no universal no-clobber guarantee, and a
     * null return does not actually promise the id was kept — only that the provider did not issue
     * a new one. Both are provider-specific. So nothing is assumed here: the document is queried
     * afterwards, and a rename that did not visibly take effect is an error rather than a success.
     *
     * <p>The no-clobber property itself is guaranteed by restricting the destination to
     * {@link DocumentIds#EXTERNAL_STORAGE_AUTHORITY} (see
     * {@link #requireTrustedDestination}), whose implementation refuses a colliding rename instead
     * of replacing the existing file.
     */
    @Override
    public String rename(String documentId, String displayName) throws StoreException {
        String resultId;
        try {
            Uri renamed = DocumentsContract.renameDocument(
                    resolver, Uri.parse(documentId), displayName);
            resultId = renamed == null ? documentId : renamed.toString();
        } catch (FileNotFoundException e) {
            throw new StoreException("The file to rename is gone.", e);
        } catch (Exception e) {
            throw new StoreException("Could not rename to \"" + displayName + "\".", e);
        }

        DocumentNode after;
        try {
            after = node(resultId);
        } catch (StoreException e) {
            // A rename whose result cannot be read back is not a rename we can report as done.
            throw new StoreException(
                    "The drive accepted the rename to \"" + displayName
                            + "\" but the file could not be found afterwards.", e);
        }
        if (after.name() == null || after.name().isEmpty()) {
            throw new StoreException("The drive did not report a name for the renamed file.");
        }
        return resultId;
    }

    /**
     * Refuses a destination this app cannot make safety promises about.
     *
     * <p>The engine's "never overwrite" contract depends on a rename that fails rather than
     * replaces. The system external-storage provider — which is what a mounted USB or SD volume,
     * and internal storage, are exposed through — behaves that way. An arbitrary cloud or
     * third-party provider does not have to, and this app has no way to test one at runtime
     * without risking the user's data. The stated scope is a locally mounted drive, so anything
     * else is rejected up front with an explanation.
     */
    public static void requireTrustedDestination(String destinationId) throws StoreException {
        String authority;
        try {
            authority = Uri.parse(destinationId).getAuthority();
        } catch (Exception e) {
            throw new StoreException("That destination folder could not be understood.", e);
        }
        if (!DocumentIds.EXTERNAL_STORAGE_AUTHORITY.equals(authority)) {
            throw new StoreException(
                    "This app can only copy onto storage that Android has mounted itself — a USB "
                            + "drive, an SD card, or internal storage. The folder you picked is "
                            + "provided by another app, which cannot promise that saving a file "
                            + "will not replace one already there.");
        }
    }

    @Override
    public void delete(String documentId) throws StoreException {
        try {
            if (!DocumentsContract.deleteDocument(resolver, Uri.parse(documentId))) {
                throw new StoreException("The drive refused to remove a temporary file.");
            }
        } catch (StoreException e) {
            throw e;
        } catch (Exception e) {
            throw new StoreException("Could not remove a temporary file.", e);
        }
    }

    @Override
    public boolean isSameOrDescendant(String ancestorId, String candidateId) throws StoreException {
        if (ancestorId.equals(candidateId)) {
            return true;
        }
        Uri ancestor = Uri.parse(ancestorId);
        Uri candidate = Uri.parse(candidateId);
        String ancestorAuthority = ancestor.getAuthority();
        if (ancestorAuthority == null || !ancestorAuthority.equals(candidate.getAuthority())) {
            // Different providers cannot contain one another.
            return false;
        }

        // Identity is authority + document id. The URI strings themselves are not comparable: the
        // same folder reached through two different tree grants has two different URIs, so an
        // equality test on the strings would call a folder "not the same" as itself.
        String ancestorDoc;
        String candidateDoc;
        try {
            ancestorDoc = DocumentsContract.getDocumentId(ancestor);
            candidateDoc = DocumentsContract.getDocumentId(candidate);
        } catch (Exception e) {
            throw new StoreException(
                    "One of the selected folders could not be identified, so the app cannot check "
                            + "that they do not overlap.", e);
        }

        DocumentIds.Relationship known =
                DocumentIds.relate(ancestorAuthority, ancestorDoc, candidateDoc);
        if (known == DocumentIds.Relationship.SAME
                || known == DocumentIds.Relationship.DESCENDANT) {
            return true;
        }

        // Ask the provider itself. This is authoritative where it is implemented, and it is the
        // only way to resolve providers whose ids this app does not claim to understand.
        try {
            if (DocumentsContract.isChildDocument(resolver, ancestor, candidate)) {
                return true;
            }
            if (known == DocumentIds.Relationship.UNRELATED) {
                return false;
            }
        } catch (Exception e) {
            if (known == DocumentIds.Relationship.UNRELATED) {
                // The id format is understood and the paths provably diverge; no need for the
                // provider's opinion.
                return false;
            }
            throw new StoreException(
                    "The storage provider could not say whether these two folders overlap, so the "
                            + "app will not risk copying a folder into itself.", e);
        }

        if (known == DocumentIds.Relationship.UNKNOWN) {
            // isChildDocument returned false, but for an opaque id format that answer is only
            // meaningful if the provider really implements it — and a provider that does not is
            // allowed to return false. Fail closed rather than guess that the ids are path-shaped.
            throw new StoreException(
                    "This storage provider does not describe how its folders are nested, so the "
                            + "app cannot prove the two folders do not overlap. Pick folders on "
                            + "internal storage, an SD card, or a USB drive.");
        }
        return false;
    }

    /**
     * A cursor that is still loading, or that carries a provider error, describes an incomplete
     * listing. Accepting it would let the app believe a folder is emptier than it is.
     */
    private static void requireComplete(Cursor cursor) throws StoreException {
        Bundle extras = cursor.getExtras();
        if (extras == null) {
            return;
        }
        if (extras.getBoolean(DocumentsContract.EXTRA_LOADING, false)) {
            throw new StoreException(
                    "The storage provider is still loading this folder. Try again in a moment.");
        }
        String error = extras.getString(DocumentsContract.EXTRA_ERROR);
        if (error != null && !error.isEmpty()) {
            throw new StoreException("The storage provider reported: " + error);
        }
    }

    private static DocumentNode fromCursor(Uri uri, Cursor cursor) {
        String name = cursor.isNull(1) ? "" : cursor.getString(1);
        String mime = cursor.isNull(2) ? null : cursor.getString(2);
        long size = cursor.isNull(3) ? -1L : cursor.getLong(3);
        long modified = cursor.isNull(4) ? 0L : cursor.getLong(4);
        boolean directory = DocumentsContract.Document.MIME_TYPE_DIR.equals(mime);
        return new DocumentNode(uri.toString(), name, mime, directory ? -1L : size, modified,
                directory);
    }

    private static String safeMime(String mimeType) {
        if (mimeType == null || mimeType.isEmpty()
                || DocumentsContract.Document.MIME_TYPE_DIR.equals(mimeType)) {
            return "application/octet-stream";
        }
        return mimeType;
    }
}
