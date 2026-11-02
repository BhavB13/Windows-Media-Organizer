package com.bhavb13.dtm.engine;

import java.util.concurrent.atomic.AtomicBoolean;

/** Cooperative cancellation flag, polled between chunks and between files. */
public interface Cancellation {

    boolean isCancelled();

    Cancellation NONE = () -> false;

    /** Simple thread-safe flag for callers that need to trip cancellation from another thread. */
    final class Flag implements Cancellation {
        private final AtomicBoolean cancelled = new AtomicBoolean(false);

        public void cancel() {
            cancelled.set(true);
        }

        @Override
        public boolean isCancelled() {
            return cancelled.get();
        }
    }
}
