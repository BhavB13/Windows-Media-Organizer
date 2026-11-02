package com.bhavb13.dtm.saf;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import android.Manifest;
import android.app.UiAutomation;
import android.content.ContentResolver;
import android.net.Uri;
import android.provider.DocumentsContract;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import com.bhavb13.dtm.engine.Cancellation;
import com.bhavb13.dtm.engine.DocumentNode;
import com.bhavb13.dtm.engine.ItemResult;
import com.bhavb13.dtm.engine.Names;
import com.bhavb13.dtm.engine.ProgressListener;
import com.bhavb13.dtm.engine.StoreException;
import com.bhavb13.dtm.engine.TransferEngine;
import com.bhavb13.dtm.engine.TransferResult;

import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.UUID;

/**
 * {@link SafDocumentStore} against the real system external-storage provider on a device.
 *
 * <p>The JVM tests use an in-memory fake, so they cannot see how {@code DocumentsContract} and
 * {@code ExternalStorageProvider} actually behave: how tree URIs are built, what a colliding
 * create or rename really does, what id a renamed document keeps. Those are exactly the behaviours
 * the engine's "never overwrite" promise rests on, so they are measured here.
 *
 * <p>Access comes from adopting the shell's {@code MANAGE_DOCUMENTS} permission instead of a
 * tree grant from the system picker, which cannot be driven reliably from a test. Everything the
 * store does after the grant is identical; only the grant itself is not exercised. Each test works
 * inside its own uniquely named folder on primary storage and removes it afterwards.
 */
@RunWith(AndroidJUnit4.class)
public class SafDocumentStoreInstrumentedTest {

    private static final String AUTHORITY = DocumentIds.EXTERNAL_STORAGE_AUTHORITY;

    private UiAutomation automation;
    private ContentResolver resolver;
    private SafDocumentStore store;

    /** Document id of this test's scratch folder, e.g. "primary:DtmTest-1a2b3c". */
    private String scratchDocId;
    /** Tree-scoped document URI for the scratch folder, as the app would hold it. */
    private String scratch;

    @Before
    public void setUp() throws Exception {
        automation = InstrumentationRegistry.getInstrumentation().getUiAutomation();
        automation.adoptShellPermissionIdentity(Manifest.permission.MANAGE_DOCUMENTS);
        resolver = InstrumentationRegistry.getInstrumentation().getTargetContext()
                .getContentResolver();
        store = new SafDocumentStore(resolver);

        Uri volumeRoot = treeDocument("primary:");
        String name = "DtmTest-" + UUID.randomUUID().toString().substring(0, 8);
        Uri created = DocumentsContract.createDocument(resolver, volumeRoot,
                DocumentsContract.Document.MIME_TYPE_DIR, name);
        assertNotNull("Could not create the scratch folder on primary storage", created);
        scratchDocId = DocumentsContract.getDocumentId(created);
        scratch = SafDocumentStore.rootIdForTree(
                DocumentsContract.buildTreeDocumentUri(AUTHORITY, scratchDocId));
    }

    @After
    public void tearDown() throws Exception {
        try {
            if (scratchDocId != null) {
                DocumentsContract.deleteDocument(resolver, treeDocument(scratchDocId));
            }
        } finally {
            automation.dropShellPermissionIdentity();
        }
    }

    // ------------------------------------------------------------------ helpers

    /** A document URI scoped to a tree rooted at that same document. */
    private static Uri treeDocument(String documentId) {
        return DocumentsContract.buildDocumentUriUsingTree(
                DocumentsContract.buildTreeDocumentUri(AUTHORITY, documentId), documentId);
    }

    private String writeFile(String parentId, String mime, String name, String content)
            throws Exception {
        String id = store.createUniqueDocument(parentId, mime, name);
        try (OutputStream out = store.openWrite(id)) {
            out.write(content.getBytes(StandardCharsets.UTF_8));
        }
        return id;
    }

    private String read(String documentId) throws Exception {
        try (InputStream in = store.openRead(documentId)) {
            ByteArrayOutputStream buffer = new ByteArrayOutputStream();
            byte[] chunk = new byte[4096];
            int n;
            while ((n = in.read(chunk)) != -1) {
                buffer.write(chunk, 0, n);
            }
            return buffer.toString(StandardCharsets.UTF_8.name());
        }
    }

    private List<String> namesIn(String directoryId) throws Exception {
        List<String> names = new ArrayList<>();
        for (DocumentNode child : store.listChildren(directoryId)) {
            names.add(child.name());
        }
        Collections.sort(names);
        return names;
    }

    // ------------------------------------------------------------- basic calls

    @Test
    public void rootIdForTreeNamesTheGrantedFolder() throws Exception {
        DocumentNode root = store.node(scratch);

        assertTrue(root.isDirectory());
        assertEquals(scratchDocId.substring("primary:".length()), root.name());
        assertEquals(scratchDocId, DocumentsContract.getDocumentId(Uri.parse(scratch)));
    }

    @Test
    public void listsEveryChildWithTypeAndSize() throws Exception {
        writeFile(scratch, "text/plain", "a.txt", "alpha");
        writeFile(scratch, "application/octet-stream", "b.bin", "bravo!");
        store.createOrOpenDirectory(scratch, "sub");

        List<DocumentNode> children = store.listChildren(scratch);

        assertEquals(3, children.size());
        for (DocumentNode child : children) {
            switch (child.name()) {
                case "a.txt":
                    assertFalse(child.isDirectory());
                    assertEquals(5L, child.size());
                    break;
                case "b.bin":
                    assertFalse(child.isDirectory());
                    assertEquals(6L, child.size());
                    break;
                case "sub":
                    assertTrue(child.isDirectory());
                    break;
                default:
                    fail("Unexpected child " + child.name());
            }
        }
    }

    @Test
    public void childIdsAreUsableForFurtherCalls() throws Exception {
        String sub = store.createOrOpenDirectory(scratch, "sub");
        writeFile(sub, "text/plain", "inner.txt", "deep");

        DocumentNode listed = store.childByName(scratch, "sub");
        assertNotNull(listed);
        DocumentNode inner = store.childByName(listed.id(), "inner.txt");

        assertNotNull("A listed child's id must itself be listable", inner);
        assertEquals("deep", read(inner.id()));
        assertNull(store.childByName(scratch, "missing"));
    }

    @Test
    public void createOrOpenDirectoryReturnsTheExistingFolder() throws Exception {
        String first = store.createOrOpenDirectory(scratch, "photos");
        String second = store.createOrOpenDirectory(scratch, "photos");

        assertEquals(DocumentsContract.getDocumentId(Uri.parse(first)),
                DocumentsContract.getDocumentId(Uri.parse(second)));
        assertEquals(List.of("photos"), namesIn(scratch));
    }

    @Test
    public void createOrOpenDirectoryRefusesANameHeldByAFile() throws Exception {
        writeFile(scratch, "application/octet-stream", "photos", "not a folder");

        try {
            store.createOrOpenDirectory(scratch, "photos");
            fail("A file must not be mistaken for, or replaced by, a folder");
        } catch (StoreException expected) {
            // expected
        }
        assertEquals(List.of("photos"), namesIn(scratch));
    }

    @Test
    public void openWriteStartsFromOffsetZero() throws Exception {
        String id = writeFile(scratch, "text/plain", "retry.txt", "a much longer first attempt");

        try (OutputStream out = store.openWrite(id)) {
            out.write("short".getBytes(StandardCharsets.UTF_8));
        }

        assertEquals("A retried write must not leave the tail of the previous one",
                "short", read(id));
    }

    @Test
    public void deletedDocumentIsReportedGoneEverywhere() throws Exception {
        String id = writeFile(scratch, "text/plain", "gone.txt", "bye");

        store.delete(id);

        assertEquals(List.of(), namesIn(scratch));
        try {
            store.node(id);
            fail("node() on a deleted document must throw");
        } catch (StoreException expected) {
            // expected
        }
        try {
            store.openRead(id).close();
            fail("openRead() on a deleted document must throw");
        } catch (StoreException expected) {
            // expected
        }
    }

    // ------------------------------------------------- the no-overwrite promise

    @Test
    public void createUniqueDocumentNeverReusesAnExistingName() throws Exception {
        String original = writeFile(scratch, "text/plain", "notes.txt", "the user's file");

        String second = store.createUniqueDocument(scratch, "text/plain", "notes.txt");

        assertNotEquals(DocumentsContract.getDocumentId(Uri.parse(original)),
                DocumentsContract.getDocumentId(Uri.parse(second)));
        assertNotEquals("notes.txt", store.node(second).name());
        assertEquals("The existing document must not be truncated",
                "the user's file", read(original));
        assertEquals(2, store.listChildren(scratch).size());
    }

    @Test
    public void renameOntoAnExistingNameNeverReplacesIt() throws Exception {
        String existing = writeFile(scratch, "text/plain", "notes.txt", "already on the drive");
        String partial = writeFile(scratch, "text/plain", "other.txt", "from the phone");

        String resultId = null;
        try {
            resultId = store.rename(partial, "notes.txt");
        } catch (StoreException refused) {
            // Refusing is one acceptable answer.
        }

        if (resultId != null) {
            // Choosing a different name is the other.
            assertNotEquals("notes.txt", store.node(resultId).name());
            assertEquals("from the phone", read(resultId));
        }
        DocumentNode survivor = store.childByName(scratch, "notes.txt");
        assertNotNull(survivor);
        assertEquals("The file that held the name must be untouched",
                "already on the drive", read(survivor.id()));
        assertEquals("already on the drive", read(existing));
        assertEquals(2, store.listChildren(scratch).size());
    }

    @Test
    public void renameToAFreeNameIsVisibleAndReturnsAUsableId() throws Exception {
        String partial = writeFile(scratch, "text/plain", "clip.txt" + Names.PARTIAL_SUFFIX,
                "content");

        String published = store.rename(partial, "clip.txt");

        assertEquals("clip.txt", store.node(published).name());
        assertEquals("content", read(published));
        assertEquals(List.of("clip.txt"), namesIn(scratch));
    }

    /**
     * The engine recognises an interrupted copy by the ".dtmpart" suffix, so it creates partials
     * untyped. This pins down why: a typed partial has the type's extension appended by the
     * provider, while an untyped one keeps exactly the name asked for.
     */
    @Test
    public void onlyUntypedPartialsKeepTheirSuffix() throws Exception {
        String typed = store.createUniqueDocument(scratch, "image/jpeg",
                "IMG_0001.jpg" + Names.PARTIAL_SUFFIX);
        assertEquals("IMG_0001.jpg" + Names.PARTIAL_SUFFIX + ".jpg", store.node(typed).name());

        for (String name : new String[]{"IMG_0002.jpg", "VID_0001.mp4", "notes.txt", "blob"}) {
            String requested = name + Names.PARTIAL_SUFFIX;
            String id = store.createUniqueDocument(scratch, "application/octet-stream", requested);
            assertEquals(requested, store.node(id).name());
        }
    }

    @Test
    public void renamingAnUntypedPartialRestoresTheRealType() throws Exception {
        String partial = writeFile(scratch, "application/octet-stream",
                "IMG_0001.jpg" + Names.PARTIAL_SUFFIX, "photo bytes");

        String published = store.rename(partial, "IMG_0001.jpg");

        DocumentNode node = store.node(published);
        assertEquals("IMG_0001.jpg", node.name());
        assertEquals("image/jpeg", node.mimeType());
    }

    // ---------------------------------------------------------------- overlap

    @Test
    public void sameFolderThroughTwoGrantsIsTheSame() throws Exception {
        String sub = store.createOrOpenDirectory(scratch, "DCIM");
        String subDocId = DocumentsContract.getDocumentId(Uri.parse(sub));
        String viaOwnGrant = SafDocumentStore.rootIdForTree(
                DocumentsContract.buildTreeDocumentUri(AUTHORITY, subDocId));
        assertNotEquals("Precondition: the two URIs differ as strings", sub, viaOwnGrant);

        assertTrue(store.isSameOrDescendant(sub, viaOwnGrant));
        assertTrue(store.isSameOrDescendant(viaOwnGrant, sub));
    }

    @Test
    public void descendantsAreRecognisedAndSiblingsWithASharedPrefixAreNot() throws Exception {
        String dcim = store.createOrOpenDirectory(scratch, "DCIM");
        String camera = store.createOrOpenDirectory(dcim, "Camera");
        String dcim2 = store.createOrOpenDirectory(scratch, "DCIM2");

        assertTrue(store.isSameOrDescendant(scratch, camera));
        assertTrue(store.isSameOrDescendant(dcim, camera));
        assertFalse(store.isSameOrDescendant(camera, dcim));
        assertFalse(store.isSameOrDescendant(dcim, dcim2));
        assertFalse(store.isSameOrDescendant(dcim2, dcim));
    }

    @Test
    public void volumeRootContainsEverythingOnIt() throws Exception {
        String volumeRoot = SafDocumentStore.rootIdForTree(
                DocumentsContract.buildTreeDocumentUri(AUTHORITY, "primary:"));

        assertTrue(store.isSameOrDescendant(volumeRoot, scratch));
        assertFalse(store.isSameOrDescendant(scratch, volumeRoot));
    }

    @Test
    public void trustedDestinationAcceptsOnlyTheExternalStorageProvider() throws Exception {
        SafDocumentStore.requireTrustedDestination(scratch);

        String other = DocumentsContract.buildDocumentUriUsingTree(
                DocumentsContract.buildTreeDocumentUri(
                        "com.android.providers.downloads.documents", "downloads"),
                "downloads").toString();
        try {
            SafDocumentStore.requireTrustedDestination(other);
            fail("A provider other than external storage must be refused");
        } catch (StoreException expected) {
            // expected
        }
    }

    // ------------------------------------------------------ engine end to end

    @Test
    public void engineCopiesOnceKeepsClashesAndLeavesNoPartials() throws Exception {
        String phone = store.createOrOpenDirectory(scratch, "phone");
        String usb = store.createOrOpenDirectory(scratch, "usb");
        writeFile(phone, "image/jpeg", "IMG_0001.jpg", "photo bytes");
        writeFile(phone, "text/plain", "notes.txt", "from the phone");
        String nested = store.createOrOpenDirectory(phone, "Camera");
        writeFile(nested, "video/mp4", "VID_0001.mp4", "video bytes");
        writeFile(nested, "image/jpeg", "copy-of-photo.jpg", "photo bytes");
        writeFile(usb, "text/plain", "notes.txt", "already on the drive");

        TransferEngine engine = new TransferEngine(store);
        TransferResult first = engine.transfer(phone, usb, ProgressListener.NONE,
                Cancellation.NONE);

        assertEquals(first.failures().toString(), 0, first.failed());
        assertEquals(3, first.copied());
        assertEquals("Identical content within one batch is copied once",
                1, first.skippedDuplicates());
        assertEquals(List.of("Camera", "IMG_0001.jpg", "notes (1).txt", "notes.txt"),
                namesIn(usb));
        assertEquals("already on the drive",
                read(store.childByName(usb, "notes.txt").id()));
        assertEquals("from the phone", read(store.childByName(usb, "notes (1).txt").id()));
        DocumentNode usbCamera = store.childByName(usb, "Camera");
        assertEquals(List.of("VID_0001.mp4"), namesIn(usbCamera.id()));

        TransferResult second = engine.transfer(phone, usb, ProgressListener.NONE,
                Cancellation.NONE);

        assertEquals(second.failures().toString(), 0, second.failed());
        assertEquals("A re-run must copy nothing", 0, second.copied());
        assertEquals(4, second.skippedDuplicates());
        assertEquals(List.of("Camera", "IMG_0001.jpg", "notes (1).txt", "notes.txt"),
                namesIn(usb));

        for (ItemResult item : first.items()) {
            if (item.writtenName() != null) {
                assertFalse(item.writtenName().endsWith(Names.PARTIAL_SUFFIX));
            }
        }
        assertEquals("The source must never be written to",
                List.of("Camera", "IMG_0001.jpg", "notes.txt"), namesIn(phone));
    }
}
