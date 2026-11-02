package com.bhavb13.dtm.saf;

/**
 * Pure reasoning about Storage Access Framework document ids, with no Android classes involved so
 * it can be unit-tested on a plain JVM.
 *
 * <p>Comparing document <em>URIs</em> as strings is wrong: the same file reached through two
 * different tree grants has two different URIs, because a tree-scoped URI carries the granted tree
 * id as well as the document id. Only the authority plus the document id identify a document.
 *
 * <p>Document ids are otherwise opaque. This class therefore only claims to understand ids from
 * providers whose id format is documented and stable, and says so honestly for everything else
 * rather than guessing that an id is path-shaped.
 */
public final class DocumentIds {

    /**
     * The system provider for internal storage, SD cards, and mounted USB volumes. Its document
     * ids are {@code "<volumeId>:<relative/path>"}, e.g. {@code "primary:DCIM/Camera"} or
     * {@code "14E9-3B0F:Photos"}. The volume root has an empty path: {@code "primary:"}.
     */
    public static final String EXTERNAL_STORAGE_AUTHORITY = "com.android.externalstorage.documents";

    /** What one document is to another. */
    public enum Relationship {
        /** The two ids name the same document. */
        SAME,
        /** The candidate lives somewhere beneath the ancestor. */
        DESCENDANT,
        /** Provably neither: different volumes, or diverging paths. */
        UNRELATED,
        /** The id format is not one this app claims to understand. Callers must fail closed. */
        UNKNOWN
    }

    private DocumentIds() {
    }

    /**
     * Decides how two documents of the same authority relate.
     *
     * @param authority the provider authority both ids come from
     * @param ancestorDocId the possible ancestor's document id
     * @param candidateDocId the document id being tested
     */
    public static Relationship relate(String authority, String ancestorDocId,
                                      String candidateDocId) {
        if (authority == null || ancestorDocId == null || candidateDocId == null) {
            return Relationship.UNKNOWN;
        }
        if (ancestorDocId.equals(candidateDocId)) {
            return Relationship.SAME;
        }
        if (!EXTERNAL_STORAGE_AUTHORITY.equals(authority)) {
            // Media, Downloads, and third-party providers use numeric or otherwise opaque ids where
            // a prefix match means nothing. Refuse to answer instead of inventing a hierarchy.
            return Relationship.UNKNOWN;
        }

        int ancestorColon = ancestorDocId.indexOf(':');
        int candidateColon = candidateDocId.indexOf(':');
        if (ancestorColon < 0 || candidateColon < 0) {
            return Relationship.UNKNOWN;
        }
        String ancestorVolume = ancestorDocId.substring(0, ancestorColon);
        String candidateVolume = candidateDocId.substring(0, candidateColon);
        if (!ancestorVolume.equals(candidateVolume)) {
            // A separate volume cannot contain, or be contained by, this one.
            return Relationship.UNRELATED;
        }

        String ancestorPath = trimSlashes(ancestorDocId.substring(ancestorColon + 1));
        String candidatePath = trimSlashes(candidateDocId.substring(candidateColon + 1));
        if (ancestorPath.equals(candidatePath)) {
            // Same document reached through different grants, e.g. "primary:DCIM" and
            // "primary:DCIM/" — the URIs differ, the documents do not.
            return Relationship.SAME;
        }
        if (ancestorPath.isEmpty()) {
            // The volume root. Note there is no slash after the colon, so a naive
            // "startsWith(ancestor + '/')" test would miss every file on the volume.
            return Relationship.DESCENDANT;
        }
        return candidatePath.startsWith(ancestorPath + "/")
                ? Relationship.DESCENDANT
                : Relationship.UNRELATED;
    }

    private static String trimSlashes(String path) {
        int start = 0;
        int end = path.length();
        while (start < end && path.charAt(start) == '/') {
            start++;
        }
        while (end > start && path.charAt(end - 1) == '/') {
            end--;
        }
        return path.substring(start, end);
    }
}
