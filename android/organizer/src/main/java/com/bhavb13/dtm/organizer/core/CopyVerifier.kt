package com.bhavb13.dtm.organizer.core

import java.util.Locale

/**
 * Pure Kotlin verification decision logic for safe copies.
 *
 * Safety contract:
 * - A copied document only counts as successful when the SHA-256 digest of the written bytes
 *   matches the source file digest exactly.
 * - On mismatch, the just-created destination document must be deleted and the error reported.
 */
object CopyVerifier {

    sealed class VerificationOutcome {
        object Match : VerificationOutcome()
        data class Mismatch(
            val sourceSha256: String,
            val destinationSha256: String
        ) : VerificationOutcome() {
            val description: String
                get() = "SHA-256 digest mismatch: source was $sourceSha256 but destination was $destinationSha256"
        }
    }

    /**
     * Compares source and destination SHA-256 hex digests (case-insensitive).
     */
    fun verify(sourceSha256: String, destinationSha256: String): VerificationOutcome {
        val s = sourceSha256.trim().lowercase(Locale.ROOT)
        val d = destinationSha256.trim().lowercase(Locale.ROOT)
        return if (s.isNotEmpty() && s == d) {
            VerificationOutcome.Match
        } else {
            VerificationOutcome.Mismatch(s, d)
        }
    }
}
