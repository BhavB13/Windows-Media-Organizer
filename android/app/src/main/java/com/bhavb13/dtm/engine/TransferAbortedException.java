package com.bhavb13.dtm.engine;

/**
 * Raised when the whole run must stop rather than continue with a wrong answer: the destination
 * could not be fully enumerated, a root disappeared, or the two roots overlap.
 *
 * <p>This is deliberately distinct from a per-file failure. Anything that would make "no duplicate
 * found" untrustworthy aborts the run instead of copying.
 */
public class TransferAbortedException extends Exception {

    public TransferAbortedException(String message) {
        super(message);
    }

    public TransferAbortedException(String message, Throwable cause) {
        super(message, cause);
    }
}
