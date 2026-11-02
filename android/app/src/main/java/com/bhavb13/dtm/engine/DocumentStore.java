package com.bhavb13.dtm.engine;

import java.io.InputStream;
import java.io.OutputStream;
import java.util.List;

/**
 * The engine's entire view of storage.
 *
 * <p>Everything the transfer engine does goes through this interface, so the engine compiles and
 * runs on a plain JVM with no Android classes on the classpath. The Android implementation is
 * {@code com.bhavb13.dtm.saf.SafDocumentStore}; the tests use an in-memory fake.
 *
 * <p>Contract notes that the engine relies on for safety:
 * <ul>
 *   <li>{@link #listChildren} returns a <em>complete</em> listing or throws.</li>
 *   <li>{@link #createUniqueDocument} never returns an existing document and never truncates one;
 *       it creates a brand new document, possibly under an altered name.</li>
 *   <li>{@link #rename} never overwrites an existing sibling; it throws instead.</li>
 * </ul>
 */
public interface DocumentStore {

    /** Metadata for one document. Throws if it no longer exists. */
    DocumentNode node(String documentId) throws StoreException;

    /** Complete listing of a directory's direct children, or an exception. Never partial. */
    List<DocumentNode> listChildren(String directoryId) throws StoreException;

    /** The child of {@code directoryId} with exactly this display name, or null. */
    DocumentNode childByName(String directoryId, String displayName) throws StoreException;

    InputStream openRead(String documentId) throws StoreException;

    /** Opens a document for writing from offset zero. Only ever used on documents we created. */
    OutputStream openWrite(String documentId) throws StoreException;

    /**
     * Creates a new document under {@code parentId}. Must not collide with, replace, or truncate
     * any existing document; the provider may pick a different display name than requested.
     *
     * @return the id of the newly created document
     */
    String createUniqueDocument(String parentId, String mimeType, String displayName)
            throws StoreException;

    /** Returns the existing child directory with this name, creating it if absent. */
    String createOrOpenDirectory(String parentId, String displayName) throws StoreException;

    /**
     * Renames a document. Must fail rather than replace an existing sibling of the same name.
     *
     * @return the document id after the rename (providers may issue a new id)
     */
    String rename(String documentId, String displayName) throws StoreException;

    /** Deletes a document. The engine only ever calls this for partials it created itself. */
    void delete(String documentId) throws StoreException;

    /**
     * True when {@code candidateId} is {@code ancestorId} or lives underneath it. Used to reject a
     * destination that sits inside the source (or vice versa) before anything is copied.
     */
    boolean isSameOrDescendant(String ancestorId, String candidateId) throws StoreException;
}
