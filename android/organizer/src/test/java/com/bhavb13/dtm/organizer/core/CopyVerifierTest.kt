package com.bhavb13.dtm.organizer.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class CopyVerifierTest {

    @Test
    fun matchingDigestsReturnOutcomeMatch() {
        val digest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        val outcome = CopyVerifier.verify(digest, digest)
        assertTrue(outcome is CopyVerifier.VerificationOutcome.Match)
    }

    @Test
    fun matchingDigestsCaseInsensitiveReturnOutcomeMatch() {
        val lower = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        val upper = "E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855"
        val outcome = CopyVerifier.verify(lower, upper)
        assertTrue(outcome is CopyVerifier.VerificationOutcome.Match)
    }

    @Test
    fun mismatchingDigestsReturnOutcomeMismatch() {
        val src = "1111111111111111111111111111111111111111111111111111111111111111"
        val dst = "2222222222222222222222222222222222222222222222222222222222222222"
        val outcome = CopyVerifier.verify(src, dst)
        assertTrue(outcome is CopyVerifier.VerificationOutcome.Mismatch)
        val mismatch = outcome as CopyVerifier.VerificationOutcome.Mismatch
        assertEquals(src, mismatch.sourceSha256)
        assertEquals(dst, mismatch.destinationSha256)
        assertTrue(mismatch.description.contains("mismatch"))
    }

    @Test
    fun blankOrEmptyDigestIsTreatedAsMismatch() {
        val valid = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        val outcome = CopyVerifier.verify(valid, "")
        assertTrue(outcome is CopyVerifier.VerificationOutcome.Mismatch)
    }
}
