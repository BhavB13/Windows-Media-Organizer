package com.bhavb13.dtm.organizer.core

import java.io.ByteArrayInputStream
import java.io.IOException
import java.io.InputStream
import java.security.MessageDigest
import java.security.NoSuchAlgorithmException

/**
 * Full-content SHA-256 hashing utilities with cooperative cancellation.
 * Pure Kotlin / standard Java implementation with no Android imports.
 */
object Sha256Hasher {
    private const val BUFFER_SIZE = 64 * 1024
    private val HEX_CHARS = "0123456789abcdef".toCharArray()

    fun newDigest(): MessageDigest {
        return try {
            MessageDigest.getInstance("SHA-256")
        } catch (e: NoSuchAlgorithmException) {
            throw IllegalStateException("SHA-256 MessageDigest is unavailable on this JVM", e)
        }
    }

    fun hashBytes(bytes: ByteArray): String {
        val digest = newDigest()
        digest.update(bytes)
        return toHex(digest.digest())
    }

    /**
     * Streams and hashes the entire [input] stream, checking [isCancelled] before each chunk.
     * Returns the lowercase 64-character hex SHA-256 digest, or `null` if cancelled mid-stream.
     */
    @Throws(IOException::class)
    fun hashStream(
        input: InputStream,
        isCancelled: () -> Boolean = { false }
    ): String? {
        if (isCancelled()) return null
        val digest = newDigest()
        val buffer = ByteArray(BUFFER_SIZE)
        while (true) {
            if (isCancelled()) return null
            val read = input.read(buffer)
            if (read < 0) break
            if (read > 0) {
                digest.update(buffer, 0, read)
            }
        }
        return if (isCancelled()) null else toHex(digest.digest())
    }

    fun toHex(bytes: ByteArray): String {
        val out = CharArray(bytes.size * 2)
        for (i in bytes.indices) {
            val v = bytes[i].toInt() and 0xFF
            out[i * 2] = HEX_CHARS[v ushr 4]
            out[i * 2 + 1] = HEX_CHARS[v and 0x0F]
        }
        return String(out)
    }
}

/**
 * Abstraction for opening a media file's raw byte stream so [DuplicateDetector] remains completely
 * independent of Android's ContentResolver and can be tested in-memory on a plain JVM.
 */
fun interface ContentHashSource {
    @Throws(IOException::class)
    fun openStream(item: MediaItem): InputStream

    companion object {
        /**
         * Creates an in-memory [ContentHashSource] backed by a map of item IDs to raw file bytes.
         */
        fun fromByteMap(contentsById: Map<String, ByteArray>): ContentHashSource {
            return ContentHashSource { item ->
                val bytes = contentsById[item.id]
                    ?: throw IOException("No content registered for item id=${item.id}")
                ByteArrayInputStream(bytes)
            }
        }
    }
}
