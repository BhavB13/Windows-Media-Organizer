package com.bhavb13.dtm.engine;

/**
 * Progress callbacks from the engine. Invoked on the worker thread, never on a UI thread; the
 * caller is responsible for hopping threads before touching widgets.
 */
public interface ProgressListener {

    /** A coarse phase change, e.g. "Scanning destination". */
    void onPhase(String phase);

    /** Called while scanning, with the number of items seen so far. */
    void onScanProgress(String phase, int itemsSeen);

    /** Called once per file processed during a transfer. */
    void onFileProgress(int filesDone, int filesTotal, long bytesDone, long bytesTotal,
                        String currentName);

    /** Called once per file with its outcome, so a UI can build a running log. */
    void onItemResult(ItemResult result);

    ProgressListener NONE = new ProgressListener() {
        @Override
        public void onPhase(String phase) {
        }

        @Override
        public void onScanProgress(String phase, int itemsSeen) {
        }

        @Override
        public void onFileProgress(int filesDone, int filesTotal, long bytesDone, long bytesTotal,
                                   String currentName) {
        }

        @Override
        public void onItemResult(ItemResult result) {
        }
    };
}
