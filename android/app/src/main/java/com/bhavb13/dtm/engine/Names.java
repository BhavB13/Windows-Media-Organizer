package com.bhavb13.dtm.engine;

/** Display-name helpers for collision renaming. */
public final class Names {

    /** Suffix used for in-progress documents the app creates and is allowed to clean up. */
    public static final String PARTIAL_SUFFIX = ".dtmpart";

    private Names() {
    }

    /** "holiday.jpg" -> "holiday"; leading dots are treated as part of the stem. */
    public static String stem(String name) {
        int dot = name.lastIndexOf('.');
        return dot <= 0 ? name : name.substring(0, dot);
    }

    /** "holiday.jpg" -> ".jpg"; "" when there is no extension. */
    public static String extension(String name) {
        int dot = name.lastIndexOf('.');
        return dot <= 0 ? "" : name.substring(dot);
    }

    /** Candidate name for the n-th collision: n=0 is the original, n=1 is "holiday (1).jpg". */
    public static String candidate(String name, int n) {
        if (n <= 0) {
            return name;
        }
        return stem(name) + " (" + n + ")" + extension(name);
    }
}
