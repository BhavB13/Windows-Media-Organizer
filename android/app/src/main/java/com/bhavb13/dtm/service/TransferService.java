package com.bhavb13.dtm.service;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import com.bhavb13.dtm.R;
import com.bhavb13.dtm.app.TransferController;
import com.bhavb13.dtm.ui.MainActivity;

/**
 * Keeps a scan or copy alive while the user is elsewhere, and puts a cancel button in the shade.
 *
 * <p>The service does not own the work: {@link TransferController} does. The service starts it,
 * mirrors its progress into a notification, and stops itself when the controller goes idle.
 */
public final class TransferService extends Service {

    public static final String ACTION_SCAN = "com.bhavb13.dtm.action.SCAN";
    public static final String ACTION_TRANSFER = "com.bhavb13.dtm.action.TRANSFER";
    public static final String ACTION_CANCEL = "com.bhavb13.dtm.action.CANCEL";
    public static final String EXTRA_SOURCE = "source";
    public static final String EXTRA_DESTINATION = "destination";

    private static final String CHANNEL_ID = "transfer";
    private static final int NOTIFICATION_ID = 1;
    /** Backstop so a leaked wake lock cannot outlive any plausible transfer. */
    private static final long WAKE_LOCK_TIMEOUT_MS = 6L * 60L * 60L * 1000L;

    private final TransferController controller = TransferController.get();
    private NotificationManager notifications;
    private PowerManager.WakeLock wakeLock;

    private final TransferController.Observer observer = new TransferController.Observer() {
        @Override
        public void onStateChanged(TransferController.Snapshot snapshot) {
            if (snapshot.phase != TransferController.Phase.IDLE) {
                notifications.notify(NOTIFICATION_ID, buildNotification(snapshot));
            }
        }

        @Override
        public void onFinished(TransferController.Snapshot snapshot) {
            stopSelf();
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        notifications = getSystemService(NotificationManager.class);
        createChannel();
        controller.addObserver(observer);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? null : intent.getAction();
        if (ACTION_CANCEL.equals(action)) {
            controller.cancel();
            return START_NOT_STICKY;
        }

        if (!startAsForeground()) {
            // Android refused the foreground promotion. Continuing would risk the work being
            // killed mid-copy with no notification and no way to cancel it.
            releaseWakeLock();
            stopSelf();
            return START_NOT_STICKY;
        }

        String source = intent == null ? null : intent.getStringExtra(EXTRA_SOURCE);
        String destination = intent == null ? null : intent.getStringExtra(EXTRA_DESTINATION);
        if (source == null || destination == null) {
            stopSelf();
            return START_NOT_STICKY;
        }

        boolean started = ACTION_SCAN.equals(action)
                ? controller.startScan(getContentResolver(), source, destination)
                : controller.startTransfer(getContentResolver(), source, destination);
        if (!started && !controller.isBusy()) {
            // Nothing to keep the service alive for.
            stopSelf();
            return START_NOT_STICKY;
        }
        if (started) {
            // Hashing and copying gigabytes takes far longer than the screen stays on, and the
            // CPU sleeping mid-copy is what leaves a drive full of partials.
            acquireWakeLock();
        }
        // Not sticky: a restarted copy without the user asking for it would be a surprise.
        return START_NOT_STICKY;
    }

    /**
     * Android 15 caps how long a {@code dataSync} foreground service may run. When the cap is hit
     * the app must stop promptly or be killed with an ANR.
     *
     * <p>Stopping here is safe by construction: the engine publishes a file only after the whole
     * file is written and verified, so an interrupted run leaves complete files plus at most one
     * partial, and a fresh run skips everything already verified.
     */
    @Override
    public void onTimeout(int startId, int fgsType) {
        stopBecauseOfTimeout();
    }

    /** The Android 14 signature; kept so the callback also fires on API 34 devices. */
    @Override
    public void onTimeout(int startId) {
        stopBecauseOfTimeout();
    }

    private void stopBecauseOfTimeout() {
        controller.cancel();
        if (notifications != null) {
            // A silent disappearance would look like the copy finished. Say what happened.
            notifications.notify(NOTIFICATION_ID + 1,
                    buildPlainNotification(getString(R.string.notification_timeout)));
        }
        releaseWakeLock();
        stopSelf();
    }

    @Override
    public void onDestroy() {
        controller.removeObserver(observer);
        if (controller.isBusy()) {
            // Being destroyed while work is still running means the system is taking the service
            // away. Letting the worker thread carry on would keep writing to the drive with no
            // notification, no cancel button, and nothing to report the result to.
            controller.cancel();
        }
        releaseWakeLock();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    /** @return true when the service is now in the foreground and safe to do work in. */
    private boolean startAsForeground() {
        try {
            Notification notification = buildNotification(controller.snapshot());
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(NOTIFICATION_ID, notification,
                        ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
            } else {
                startForeground(NOTIFICATION_ID, notification);
            }
            return true;
        } catch (RuntimeException e) {
            // ForegroundServiceStartNotAllowedException and friends. A refusal is a condition to
            // handle, not a crash.
            return false;
        }
    }

    private void acquireWakeLock() {
        if (wakeLock != null && wakeLock.isHeld()) {
            return;
        }
        try {
            PowerManager power = getSystemService(PowerManager.class);
            if (power == null) {
                return;
            }
            wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "dtm:transfer");
            wakeLock.setReferenceCounted(false);
            // The timeout is a backstop, not the plan: onDestroy and onTimeout both release it.
            // Without one, a bug here would drain the battery instead of just being a bug.
            wakeLock.acquire(WAKE_LOCK_TIMEOUT_MS);
        } catch (RuntimeException e) {
            // The copy is slower without it but still correct; never fail the run over this.
            wakeLock = null;
        }
    }

    private void releaseWakeLock() {
        PowerManager.WakeLock held = wakeLock;
        wakeLock = null;
        if (held != null && held.isHeld()) {
            try {
                held.release();
            } catch (RuntimeException ignored) {
                // Already released by the timeout; nothing to do.
            }
        }
    }

    /** A one-off, dismissible notification used to explain why the work stopped. */
    private Notification buildPlainNotification(String text) {
        Intent open = new Intent(this, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent openIntent = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL_ID)
                .setContentTitle(getString(R.string.app_name))
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setSmallIcon(android.R.drawable.stat_sys_upload)
                .setAutoCancel(true)
                .setContentIntent(openIntent)
                .build();
    }

    private Notification buildNotification(TransferController.Snapshot snapshot) {
        Intent open = new Intent(this, MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent openIntent = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        Intent cancel = new Intent(this, TransferService.class).setAction(ACTION_CANCEL);
        PendingIntent cancelIntent = PendingIntent.getService(this, 1, cancel,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        String title = snapshot.phase == TransferController.Phase.SCANNING
                ? getString(R.string.notification_reviewing)
                : getString(R.string.notification_copying);

        Notification.Builder builder = new Notification.Builder(this, CHANNEL_ID)
                .setContentTitle(title)
                .setContentText(snapshot.status)
                .setSmallIcon(android.R.drawable.stat_sys_upload)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setContentIntent(openIntent)
                .addAction(new Notification.Action.Builder(null,
                        getString(R.string.action_cancel), cancelIntent).build());

        if (snapshot.filesTotal > 0) {
            builder.setProgress(snapshot.filesTotal, snapshot.filesDone, false);
        } else {
            builder.setProgress(0, 0, true);
        }
        return builder.build();
    }

    private void createChannel() {
        NotificationChannel channel = new NotificationChannel(CHANNEL_ID,
                getString(R.string.channel_name), NotificationManager.IMPORTANCE_LOW);
        channel.setDescription(getString(R.string.channel_description));
        channel.setShowBadge(false);
        notifications.createNotificationChannel(channel);
    }

    /** Convenience for the Activity: fire-and-forget start of a scan or a copy. */
    public static void start(Context context, String action, String source, String destination) {
        Intent intent = new Intent(context, TransferService.class)
                .setAction(action)
                .putExtra(EXTRA_SOURCE, source)
                .putExtra(EXTRA_DESTINATION, destination);
        context.startForegroundService(intent);
    }
}
