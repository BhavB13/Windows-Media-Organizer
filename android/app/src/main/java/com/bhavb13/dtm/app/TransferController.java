package com.bhavb13.dtm.app;

import android.content.ContentResolver;
import android.os.Handler;
import android.os.Looper;

import com.bhavb13.dtm.engine.Cancellation;
import com.bhavb13.dtm.engine.ItemResult;
import com.bhavb13.dtm.engine.ProgressListener;
import com.bhavb13.dtm.engine.ScanResult;
import com.bhavb13.dtm.engine.TransferAbortedException;
import com.bhavb13.dtm.engine.TransferEngine;
import com.bhavb13.dtm.engine.TransferResult;
import com.bhavb13.dtm.saf.SafDocumentStore;

import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Process-scoped owner of the current scan or transfer.
 *
 * <p>The work outlives any Activity, so rotating the phone or briefly leaving the app cannot
 * interrupt a copy. Observers register and unregister with the Activity lifecycle and always read
 * a fresh {@link Snapshot}, which is what keeps a destroyed Activity from being retained or from
 * receiving a callback after it is gone.
 */
public final class TransferController {

    /** Which long-running operation, if any, is in flight. */
    public enum Phase {
        IDLE, SCANNING, TRANSFERRING
    }

    /** Immutable view of the controller's state, safe to hand to the UI thread. */
    public static final class Snapshot {
        public final Phase phase;
        public final String status;
        public final int filesDone;
        public final int filesTotal;
        public final long bytesDone;
        public final long bytesTotal;
        public final ScanResult scan;
        public final TransferResult transfer;
        public final String error;
        /**
         * The folders this state describes, or null when idle and nothing has run.
         *
         * <p>Carried in the snapshot so a late callback can be matched against the folders that are
         * selected <em>now</em>. Without it, a result that arrives just after the user picked a
         * different folder would be rendered as if it described the new pair.
         */
        public final String sourceRoot;
        public final String destinationRoot;

        Snapshot(Phase phase, String status, int filesDone, int filesTotal, long bytesDone,
                 long bytesTotal, ScanResult scan, TransferResult transfer, String error,
                 String sourceRoot, String destinationRoot) {
            this.sourceRoot = sourceRoot;
            this.destinationRoot = destinationRoot;
            this.phase = phase;
            this.status = status;
            this.filesDone = filesDone;
            this.filesTotal = filesTotal;
            this.bytesDone = bytesDone;
            this.bytesTotal = bytesTotal;
            this.scan = scan;
            this.transfer = transfer;
            this.error = error;
        }
    }

    /** Callbacks are always delivered on the main thread. */
    public interface Observer {
        void onStateChanged(Snapshot snapshot);

        /** The operation finished (or failed, or was cancelled) and the app is idle again. */
        void onFinished(Snapshot snapshot);
    }

    private static final TransferController INSTANCE = new TransferController();

    public static TransferController get() {
        return INSTANCE;
    }

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor(runnable -> {
        Thread thread = new Thread(runnable, "dtm-transfer");
        thread.setDaemon(true);
        return thread;
    });
    private final CopyOnWriteArrayList<Observer> observers = new CopyOnWriteArrayList<>();

    private final Object lock = new Object();
    private Phase phase = Phase.IDLE;
    private String status = "";
    private int filesDone;
    private int filesTotal;
    private long bytesDone;
    private long bytesTotal;
    private ScanResult scan;
    private TransferResult transfer;
    private String error;
    private Cancellation.Flag cancellation;
    private String sourceRoot;
    private String destinationRoot;

    private TransferController() {
    }

    public void addObserver(Observer observer) {
        observers.addIfAbsent(observer);
        observer.onStateChanged(snapshot());
    }

    public void removeObserver(Observer observer) {
        observers.remove(observer);
    }

    public Snapshot snapshot() {
        synchronized (lock) {
            return new Snapshot(phase, status, filesDone, filesTotal, bytesDone, bytesTotal, scan,
                    transfer, error, sourceRoot, destinationRoot);
        }
    }

    public boolean isBusy() {
        synchronized (lock) {
            return phase != Phase.IDLE;
        }
    }

    /** The most recent review, or null. Cleared whenever a new operation starts. */
    public ScanResult lastScan() {
        synchronized (lock) {
            return scan;
        }
    }

    public void cancel() {
        Cancellation.Flag flag;
        synchronized (lock) {
            flag = cancellation;
        }
        if (flag != null) {
            flag.cancel();
            update("Stopping...");
        }
    }

    /**
     * Starts a review. Returns false when something is already running, which is how overlapping
     * operations on the same volumes are prevented.
     */
    public boolean startScan(ContentResolver resolver, String sourceRoot, String destinationRoot) {
        return start(Phase.SCANNING, resolver, sourceRoot, destinationRoot);
    }

    /** Starts the copy. Returns false when something is already running. */
    public boolean startTransfer(ContentResolver resolver, String sourceRoot,
                                 String destinationRoot) {
        return start(Phase.TRANSFERRING, resolver, sourceRoot, destinationRoot);
    }

    private boolean start(Phase requested, ContentResolver resolver, String sourceRoot,
                          String destinationRoot) {
        Cancellation.Flag flag = new Cancellation.Flag();
        synchronized (lock) {
            if (phase != Phase.IDLE) {
                return false;
            }
            phase = requested;
            status = requested == Phase.SCANNING ? "Starting review..." : "Starting copy...";
            filesDone = 0;
            filesTotal = 0;
            bytesDone = 0L;
            bytesTotal = 0L;
            error = null;
            transfer = null;
            // Any earlier review describes a run that is now over. Keeping it would let the UI
            // render stale counts while the new operation is still running, and — worse — let a
            // previous successful review keep authorising a copy.
            scan = null;
            this.sourceRoot = sourceRoot;
            this.destinationRoot = destinationRoot;
            cancellation = flag;
        }
        publishState();

        TransferEngine engine = new TransferEngine(new SafDocumentStore(resolver));
        ProgressListener listener = new ProgressListener() {
            @Override
            public void onPhase(String phaseText) {
                update(phaseText);
            }

            @Override
            public void onScanProgress(String phaseText, int itemsSeen) {
                synchronized (lock) {
                    status = phaseText + " (" + itemsSeen + " found)";
                }
                publishState();
            }

            @Override
            public void onFileProgress(int done, int total, long doneBytes, long totalBytes,
                                       String currentName) {
                synchronized (lock) {
                    filesDone = done;
                    filesTotal = total;
                    bytesDone = doneBytes;
                    bytesTotal = totalBytes;
                    status = currentName;
                }
                publishState();
            }

            @Override
            public void onItemResult(ItemResult result) {
                // Per-item detail is reported in the final result; nothing extra to do live.
            }
        };

        worker.execute(() -> {
            String failure = null;
            ScanResult scanResult = null;
            TransferResult transferResult = null;
            try {
                if (requested == Phase.SCANNING) {
                    scanResult = engine.scan(sourceRoot, destinationRoot, listener, flag);
                } else {
                    transferResult = engine.transfer(sourceRoot, destinationRoot, listener, flag);
                }
            } catch (TransferAbortedException e) {
                failure = e.getMessage();
            } catch (RuntimeException e) {
                failure = "Something went wrong: " + e;
            }
            synchronized (lock) {
                phase = Phase.IDLE;
                cancellation = null;
                error = failure;
                if (scanResult != null) {
                    scan = scanResult;
                }
                if (transferResult != null) {
                    transfer = transferResult;
                }
                status = failure != null ? "Stopped" : "Finished";
            }
            Snapshot finalSnapshot = snapshot();
            main.post(() -> {
                for (Observer observer : observers) {
                    observer.onStateChanged(finalSnapshot);
                    observer.onFinished(finalSnapshot);
                }
            });
        });
        return true;
    }

    private void update(String text) {
        synchronized (lock) {
            status = text;
        }
        publishState();
    }

    private void publishState() {
        Snapshot current = snapshot();
        main.post(() -> {
            for (Observer observer : observers) {
                observer.onStateChanged(current);
            }
        });
    }
}
