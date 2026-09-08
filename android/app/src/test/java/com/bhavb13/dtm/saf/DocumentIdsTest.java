package com.bhavb13.dtm.saf;

import static com.bhavb13.dtm.saf.DocumentIds.EXTERNAL_STORAGE_AUTHORITY;
import static com.bhavb13.dtm.saf.DocumentIds.Relationship.DESCENDANT;
import static com.bhavb13.dtm.saf.DocumentIds.Relationship.SAME;
import static com.bhavb13.dtm.saf.DocumentIds.Relationship.UNKNOWN;
import static com.bhavb13.dtm.saf.DocumentIds.Relationship.UNRELATED;
import static org.junit.Assert.assertEquals;

import com.bhavb13.dtm.saf.DocumentIds.Relationship;

import org.junit.Test;

/**
 * The overlap check is the last thing standing between the user and a run that copies a tree into
 * itself. It has to be right about the awkward shapes real Android hands it, and it has to say
 * "I don't know" rather than guess when the id format is not one we documented.
 */
public class DocumentIdsTest {

    private static Relationship relate(String ancestor, String candidate) {
        return DocumentIds.relate(EXTERNAL_STORAGE_AUTHORITY, ancestor, candidate);
    }

    // --- the same document, reached differently -----------------------------------------------

    @Test
    public void identicalIdsAreTheSameDocument() {
        assertEquals(SAME, relate("primary:DCIM/Camera", "primary:DCIM/Camera"));
    }

    @Test
    public void aTrailingSlashDoesNotMakeItADifferentDocument() {
        // Two grants of one folder can differ only by this, and the URIs differ either way.
        assertEquals(SAME, relate("primary:DCIM", "primary:DCIM/"));
        assertEquals(SAME, relate("primary:DCIM/", "primary:DCIM"));
    }

    @Test
    public void twoSpellingsOfTheVolumeRootAreTheSameDocument() {
        assertEquals(SAME, relate("primary:", "primary:/"));
    }

    // --- containment --------------------------------------------------------------------------

    @Test
    public void aFileBeneathTheAncestorIsADescendant() {
        assertEquals(DESCENDANT, relate("primary:DCIM", "primary:DCIM/Camera/IMG_0001.jpg"));
    }

    @Test
    public void theVolumeRootContainsEverythingOnTheVolume() {
        // Regression guard: the root has no slash after the colon, so a naive
        // startsWith(ancestor + "/") test would report UNRELATED for every file on the drive
        // and let the engine copy a volume into itself.
        assertEquals(DESCENDANT, relate("primary:", "primary:DCIM"));
        assertEquals(DESCENDANT, relate("primary:", "primary:DCIM/Camera/IMG_0001.jpg"));
    }

    @Test
    public void theVolumeRootIsRecognisedThroughATrailingSlashGrant() {
        assertEquals(DESCENDANT, relate("primary:/", "primary:DCIM"));
    }

    @Test
    public void aRemovableVolumeBehavesLikeTheInternalOne() {
        assertEquals(DESCENDANT, relate("14E9-3B0F:", "14E9-3B0F:Photos/holiday.jpg"));
        assertEquals(DESCENDANT, relate("14E9-3B0F:Photos", "14E9-3B0F:Photos/holiday.jpg"));
    }

    @Test
    public void containmentIsNotSymmetric() {
        assertEquals(UNRELATED, relate("primary:DCIM/Camera", "primary:DCIM"));
    }

    // --- provably unrelated -------------------------------------------------------------------

    @Test
    public void theUsbDriveIsNotInsideInternalStorage() {
        // The whole point of the app: phone -> drive must not look like an overlap.
        assertEquals(UNRELATED, relate("primary:DCIM", "14E9-3B0F:Backup"));
        assertEquals(UNRELATED, relate("14E9-3B0F:", "primary:DCIM"));
    }

    @Test
    public void aSiblingFolderIsNotADescendant() {
        assertEquals(UNRELATED, relate("primary:DCIM", "primary:Pictures/a.jpg"));
    }

    @Test
    public void aSharedNamePrefixIsNotContainment() {
        // "DCIM2" starts with "DCIM" but is a different folder. Only a path separator counts.
        assertEquals(UNRELATED, relate("primary:DCIM", "primary:DCIM2"));
        assertEquals(UNRELATED, relate("primary:DCIM", "primary:DCIMBackup/a.jpg"));
    }

    @Test
    public void volumeNamesAreComparedExactly() {
        assertEquals(UNRELATED, relate("primary:", "primary2:DCIM"));
        assertEquals(UNRELATED, relate("primary:", "PRIMARY:DCIM"));
    }

    // --- fail closed --------------------------------------------------------------------------

    @Test
    public void anotherProvidersIdsAreNotGuessedAt() {
        // Media and Downloads ids are numeric; Drive-style ids are opaque. A prefix match there
        // means nothing, so the honest answer is UNKNOWN and the caller must refuse the run.
        assertEquals(UNKNOWN,
                DocumentIds.relate("com.android.providers.media.documents", "image:1", "image:12"));
        assertEquals(UNKNOWN,
                DocumentIds.relate("com.android.providers.downloads.documents", "raw:/a", "raw:/a/b"));
        assertEquals(UNKNOWN, DocumentIds.relate("com.example.cloud", "root", "root/child"));
    }

    @Test
    public void anIdenticalIdIsTheSameDocumentWhicheverProviderIssuedIt() {
        // Equality needs no knowledge of the id format, so it is answered before the
        // authority check. Two grants of one opaque document must still collide.
        assertEquals(SAME, DocumentIds.relate("com.example.cloud", "abc123", "abc123"));
    }

    @Test
    public void anIdWithoutAVolumeSeparatorIsNotUnderstood() {
        assertEquals(UNKNOWN, relate("primary", "primary:DCIM"));
        assertEquals(UNKNOWN, relate("primary:", "DCIM"));
    }

    @Test
    public void nullsAreNotTreatedAsUnrelated() {
        // Returning UNRELATED here would silently permit an overlapping run.
        assertEquals(UNKNOWN, relate(null, "primary:DCIM"));
        assertEquals(UNKNOWN, relate("primary:DCIM", null));
        assertEquals(UNKNOWN, DocumentIds.relate(null, "primary:", "primary:DCIM"));
    }
}
