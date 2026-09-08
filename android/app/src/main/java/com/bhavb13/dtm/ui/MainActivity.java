package com.bhavb13.dtm.ui;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.DocumentsContract;
import android.view.View;
import android.widget.Button;
import android.widget.ProgressBar;
import android.widget.TextView;

import com.bhavb13.dtm.R;
import com.bhavb13.dtm.app.TransferController;
import com.bhavb13.dtm.engine.ItemResult;
import com.bhavb13.dtm.engine.ScanResult;
import com.bhavb13.dtm.engine.SourceFile;
import com.bhavb13.dtm.engine.StoreException;
import com.bhavb13.dtm.engine.TransferResult;
import com.bhavb13.dtm.saf.SafDocumentStore;
import com.bhavb13.dtm.service.TransferService;

import java.util.List;

/**
 * The whole UI: pick two folders, review, copy.
 *
 * <p>This Activity holds no work of its own. It observes {@link TransferController} between
 * {@code onStart} and {@code onStop}, so a rotation simply detaches one instance and attaches the
 * next to the same running operation — no retained Activity, no callback into a dead view tree.
 */
public final class MainActivity extends Activity implements TransferController.Observer {

    private static final int REQUEST_SOURCE = 101;
    private static final int REQUEST_DESTINATION = 102;
    private static final int REQUEST_NOTIFICATIONS = 103;

    private static final String PREFS = "dtm";
    private static final String KEY_SOURCE = "source_tree";
    private static final String KEY_DESTINATION = "destination_tree";
    private static final String KEY_REVIEWED = "reviewed_pair";

    private final TransferController controller = TransferController.get();

    private TextView sourceValue;
    private TextView destinationValue;
    private TextView status;
    private TextView results;
    private ProgressBar progress;
    private Button sourceButton;
    private Button destinationButton;
    private Button reviewButton;
    private Button copyButton;
    private Button cancelButton;

    private SharedPreferences prefs;
    private String sourceRoot;
    private String destinationRoot;
    /** Message shown for input problems the controller never sees (nothing picked yet, etc.). */
    private String localMessage;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        sourceValue = findViewById(R.id.value_source);
        destinationValue = findViewById(R.id.value_destination);
        status = findViewById(R.id.status);
        results = findViewById(R.id.results);
        progress = findViewById(R.id.progress);
        sourceButton = findViewById(R.id.button_source);
        destinationButton = findViewById(R.id.button_destination);
        reviewButton = findViewById(R.id.button_review);
        copyButton = findViewById(R.id.button_copy);
        cancelButton = findViewById(R.id.button_cancel);

        sourceButton.setOnClickListener(v -> pickTree(REQUEST_SOURCE));
        destinationButton.setOnClickListener(v -> pickTree(REQUEST_DESTINATION));
        reviewButton.setOnClickListener(v -> startWork(TransferService.ACTION_SCAN));
        copyButton.setOnClickListener(v -> startCopy());
        cancelButton.setOnClickListener(v -> controller.cancel());

        // Persisted grants survive reboots, so the previous choices are still usable.
        sourceRoot = prefs.getString(KEY_SOURCE, null);
        destinationRoot = prefs.getString(KEY_DESTINATION, null);
        showRoots();
        askForNotificationPermission();
    }

    @Override
    protected void onStart() {
        super.onStart();
        controller.addObserver(this);
    }

    @Override
    protected void onStop() {
        // Detaching here is what prevents callbacks arriving after this instance is gone.
        controller.removeObserver(this);
        super.onStop();
    }

    // ------------------------------------------------------------ folder pick

    private void pickTree(int requestCode) {
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
                        | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                        | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
        startActivityForResult(intent, requestCode);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (resultCode != RESULT_OK || data == null || data.getData() == null) {
            return;
        }
        Uri tree = data.getData();
        int flags = Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION;
        try {
            getContentResolver().takePersistableUriPermission(tree, flags);
        } catch (SecurityException e) {
            // The grant is still valid for this session; it just will not survive a restart.
            localMessage = "This folder can be used now, but you may have to pick it again later.";
        }
        String root = SafDocumentStore.rootIdForTree(tree);
        if (requestCode == REQUEST_SOURCE) {
            sourceRoot = root;
            prefs.edit().putString(KEY_SOURCE, root).apply();
        } else if (requestCode == REQUEST_DESTINATION) {
            try {
                // Rejected here rather than half-way through a copy, while the drive is still
                // untouched and the user is standing at the picker.
                SafDocumentStore.requireTrustedDestination(root);
            } catch (StoreException e) {
                localMessage = e.getMessage();
                render(controller.snapshot());
                return;
            }
            destinationRoot = root;
            prefs.edit().putString(KEY_DESTINATION, root).apply();
        }
        // Any earlier review no longer describes the selected pair.
        clearReviewApproval();
        showRoots();
        render(controller.snapshot());
    }

    // ---------------------------------------------------------------- actions

    private void startWork(String action) {
        localMessage = null;
        if (sourceRoot == null || destinationRoot == null) {
            localMessage = getString(R.string.status_pick_both);
            render(controller.snapshot());
            return;
        }
        if (sourceRoot.equals(destinationRoot)) {
            localMessage = getString(R.string.status_same_folder);
            render(controller.snapshot());
            return;
        }
        if (controller.isBusy()) {
            localMessage = getString(R.string.status_busy);
            render(controller.snapshot());
            return;
        }
        if (TransferService.ACTION_SCAN.equals(action)) {
            // Approval is granted by a review that *finished*, in onFinished — not by one that
            // merely started. A review that failed, aborted, or was cancelled tells the user
            // nothing about what a copy would do, so it must not unlock the copy button.
            clearReviewApproval();
        }
        results.setText("");
        try {
            TransferService.start(this, action, sourceRoot, destinationRoot);
        } catch (RuntimeException e) {
            // Starting a foreground service can be refused (background start restrictions, a
            // dying process). That is a message to the user, not a crash.
            localMessage = getString(R.string.status_could_not_start);
            render(controller.snapshot());
        }
    }

    private void startCopy() {
        // Copying is only offered after the user has reviewed this exact pair of folders.
        if (sourceRoot == null || destinationRoot == null
                || !pairKey(sourceRoot, destinationRoot)
                .equals(prefs.getString(KEY_REVIEWED, null))) {
            localMessage = getString(R.string.status_review_first);
            render(controller.snapshot());
            return;
        }
        startWork(TransferService.ACTION_TRANSFER);
    }

    /**
     * Records that a review completed for exactly these folders.
     *
     * <p>Only called with a scan that ran to completion, was not cancelled, and reported no error.
     */
    private void recordReviewApproval(String source, String destination) {
        prefs.edit().putString(KEY_REVIEWED, pairKey(source, destination)).apply();
    }

    private void clearReviewApproval() {
        prefs.edit().remove(KEY_REVIEWED).apply();
    }

    private static String pairKey(String source, String destination) {
        return source + "\n" + destination;
    }

    private void askForNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            // Only affects whether progress is visible; the copy runs either way.
            requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"},
                    REQUEST_NOTIFICATIONS);
        }
    }

    // -------------------------------------------------------------- rendering

    @Override
    public void onStateChanged(TransferController.Snapshot snapshot) {
        render(snapshot);
    }

    @Override
    public void onFinished(TransferController.Snapshot snapshot) {
        if (snapshot.scan != null
                && snapshot.error == null
                && !snapshot.scan.wasCancelled()
                && describesCurrentPair(snapshot)) {
            recordReviewApproval(snapshot.sourceRoot, snapshot.destinationRoot);
        }
        render(snapshot);
    }

    /**
     * True when a result describes the folders that are selected right now.
     *
     * <p>A result posted to the main thread just before the user picked a different folder would
     * otherwise be shown against — and, for a review, taken as approval of — the new pair.
     */
    private boolean describesCurrentPair(TransferController.Snapshot snapshot) {
        return sourceRoot != null && destinationRoot != null
                && sourceRoot.equals(snapshot.sourceRoot)
                && destinationRoot.equals(snapshot.destinationRoot);
    }

    private void showRoots() {
        sourceValue.setText(describeRoot(sourceRoot));
        destinationValue.setText(describeRoot(destinationRoot));
    }

    private String describeRoot(String root) {
        if (root == null) {
            return getString(R.string.none_selected);
        }
        try {
            // The document id is the most human-readable thing available without disk access.
            return DocumentsContract.getDocumentId(Uri.parse(root));
        } catch (Exception e) {
            return root;
        }
    }

    private void render(TransferController.Snapshot snapshot) {
        boolean busy = snapshot.phase != TransferController.Phase.IDLE;
        sourceButton.setEnabled(!busy);
        destinationButton.setEnabled(!busy);
        reviewButton.setEnabled(!busy);
        copyButton.setEnabled(!busy);
        cancelButton.setEnabled(busy);
        progress.setVisibility(busy ? View.VISIBLE : View.GONE);
        if (busy) {
            boolean determinate = snapshot.filesTotal > 0;
            progress.setIndeterminate(!determinate);
            if (determinate) {
                progress.setMax(snapshot.filesTotal);
                progress.setProgress(snapshot.filesDone);
            }
        }

        String text;
        if (localMessage != null) {
            text = localMessage;
        } else if (busy) {
            text = snapshot.status
                    + (snapshot.filesTotal > 0
                    ? " (" + snapshot.filesDone + " of " + snapshot.filesTotal + ")" : "");
        } else if (snapshot.error != null) {
            text = snapshot.error;
        } else {
            text = getString(R.string.status_idle);
        }
        status.setText(text);
        // Announce so a screen-reader user is not left waiting on a silent progress bar.
        status.announceForAccessibility(text);

        if (!busy && describesCurrentPair(snapshot)) {
            if (snapshot.transfer != null) {
                results.setText(describeTransfer(snapshot.transfer));
            } else if (snapshot.scan != null) {
                results.setText(describeScan(snapshot.scan));
            }
        }
    }

    private String describeScan(ScanResult scan) {
        StringBuilder out = new StringBuilder();
        out.append("Review\n")
                .append("Files on the phone folder: ").append(scan.sourceFileCount()).append('\n')
                .append("Files already on the drive: ").append(scan.destinationFileCount())
                .append('\n')
                .append("New files to copy: ").append(scan.newFiles().size()).append('\n')
                .append("Duplicates that will be skipped: ").append(scan.duplicateFiles().size())
                .append('\n');
        if (scan.wasCancelled()) {
            out.append("The review was stopped early, so this list is incomplete.\n");
        }
        if (scan.leftoverPartials() > 0) {
            out.append("\nNote: ").append(scan.leftoverPartials())
                    .append(" file(s) on the drive end in \".dtmpart\". They are probably left over"
                            + " from a copy that was interrupted. This app will not delete them,"
                            + " because it cannot tell them apart from your own files. You can"
                            + " remove them yourself if you do not need them.\n");
        }
        appendNames(out, "Will copy", scan.newFiles());
        if (!scan.scanFailures().isEmpty()) {
            out.append("\nCould not be read:\n");
            for (ItemResult failure : scan.scanFailures()) {
                out.append("  ").append(failure.relativePath()).append(" — ")
                        .append(failure.message()).append('\n');
            }
        }
        return out.toString();
    }

    private void appendNames(StringBuilder out, String heading, List<SourceFile> files) {
        if (files.isEmpty()) {
            return;
        }
        out.append('\n').append(heading).append(":\n");
        int shown = Math.min(files.size(), 50);
        for (int i = 0; i < shown; i++) {
            out.append("  ").append(files.get(i).relativePath()).append('\n');
        }
        if (files.size() > shown) {
            out.append("  ...and ").append(files.size() - shown).append(" more\n");
        }
    }

    private String describeTransfer(TransferResult result) {
        StringBuilder out = new StringBuilder();
        out.append("Copy finished\n")
                .append("Copied and verified: ").append(result.copied()).append('\n')
                .append("Skipped as duplicates: ").append(result.skippedDuplicates()).append('\n')
                .append("Failed: ").append(result.failed()).append('\n');
        if (result.wasCancelled()) {
            out.append("Stopped early: ").append(result.cancelledItems())
                    .append(" file(s) were not copied.\n");
        }
        List<ItemResult> failures = result.failures();
        if (!failures.isEmpty()) {
            out.append("\nProblems:\n");
            for (ItemResult failure : failures) {
                out.append("  ").append(failure.relativePath()).append(" — ")
                        .append(failure.message()).append('\n');
            }
        }
        out.append("\nNothing on the phone was changed or deleted.");
        return out.toString();
    }
}
