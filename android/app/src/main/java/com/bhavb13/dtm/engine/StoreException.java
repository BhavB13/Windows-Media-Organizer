package com.bhavb13.dtm.engine;

/**
 * Raised by a {@link DocumentStore} when an operation could not be completed.
 *
 * <p>A store must throw rather than return partial data. In particular, a directory listing that
 * could not be fully enumerated (revoked permission, unplugged USB drive, provider crash) must
 * throw so the engine can refuse to treat an incomplete destination as "nothing is there yet".
 */
public class StoreException extends Exception {

    private final boolean unavailable;

    public StoreException(String message) {
        this(message, null, false);
    }

    public StoreException(String message, Throwable cause) {
        this(message, cause, false);
    }

    public StoreException(String message, Throwable cause, boolean unavailable) {
        super(message, cause);
        this.unavailable = unavailable;
    }

    /** True when the whole volume looks gone (drive unplugged, grant revoked). */
    public boolean isUnavailable() {
        return unavailable;
    }

    public static StoreException unavailable(String message, Throwable cause) {
        return new StoreException(message, cause, true);
    }
}
