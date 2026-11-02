package com.bhavb13.dtm.engine;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

/** Collision-rename naming rules. */
public class NamesTest {

    @Test
    public void firstCandidateIsTheOriginalName() {
        assertEquals("photo.jpg", Names.candidate("photo.jpg", 0));
    }

    @Test
    public void laterCandidatesNumberTheStemAndKeepTheExtension() {
        assertEquals("photo (1).jpg", Names.candidate("photo.jpg", 1));
        assertEquals("photo (7).jpg", Names.candidate("photo.jpg", 7));
    }

    @Test
    public void handlesNamesWithoutAnExtension() {
        assertEquals("README", Names.candidate("README", 0));
        assertEquals("README (2)", Names.candidate("README", 2));
    }

    @Test
    public void treatsOnlyTheLastDotAsTheExtension() {
        assertEquals("archive.tar (1).gz", Names.candidate("archive.tar.gz", 1));
    }

    @Test
    public void keepsALeadingDotAsPartOfTheName() {
        assertEquals(".config", Names.stem(".config"));
        assertEquals("", Names.extension(".config"));
        assertEquals(".config (1)", Names.candidate(".config", 1));
    }
}
