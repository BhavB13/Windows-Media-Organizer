package com.bhavb13.dtm.engine;

import java.util.Collections;
import java.util.List;

/** Totals and per-item outcomes for one transfer run. */
public final class TransferResult {

    private final List<ItemResult> items;
    private final int copied;
    private final int skippedDuplicates;
    private final int failed;
    private final int cancelledItems;
    private final long bytesCopied;
    private final boolean cancelled;

    public TransferResult(List<ItemResult> items, long bytesCopied, boolean cancelled) {
        this.items = Collections.unmodifiableList(items);
        this.bytesCopied = bytesCopied;
        this.cancelled = cancelled;
        int c = 0;
        int s = 0;
        int f = 0;
        int x = 0;
        for (ItemResult item : items) {
            switch (item.status()) {
                case COPIED:
                    c++;
                    break;
                case SKIPPED_DUPLICATE:
                    s++;
                    break;
                case FAILED:
                    f++;
                    break;
                case CANCELLED:
                default:
                    x++;
                    break;
            }
        }
        this.copied = c;
        this.skippedDuplicates = s;
        this.failed = f;
        this.cancelledItems = x;
    }

    public List<ItemResult> items() {
        return items;
    }

    /** Files whose written bytes were re-read and verified to match the source digest. */
    public int copied() {
        return copied;
    }

    public int skippedDuplicates() {
        return skippedDuplicates;
    }

    public int failed() {
        return failed;
    }

    public int cancelledItems() {
        return cancelledItems;
    }

    public long bytesCopied() {
        return bytesCopied;
    }

    public boolean wasCancelled() {
        return cancelled;
    }

    /** Only failures, for a compact UI message list. */
    public List<ItemResult> failures() {
        java.util.ArrayList<ItemResult> out = new java.util.ArrayList<>();
        for (ItemResult item : items) {
            if (item.status() == ItemResult.Status.FAILED) {
                out.add(item);
            }
        }
        return out;
    }
}
