package com.bhavb13.dtm.organizer.data

import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Environment
import android.os.storage.StorageManager
import android.os.storage.StorageVolume
import com.bhavb13.dtm.organizer.core.UsbVolumeInfo

/**
 * Detects removable storage volumes (USB OTG drives, SD cards) and builds access intents.
 */
object UsbStorageHelper {

    /**
     * Enumerates all removable storage volumes reported by Android's [StorageManager].
     */
    fun getRemovableVolumes(context: Context): List<UsbVolumeInfo> {
        val storageManager = context.getSystemService(StorageManager::class.java) ?: return emptyList()
        val volumes = storageManager.storageVolumes
        val result = mutableListOf<UsbVolumeInfo>()

        for (volume in volumes) {
            if (volume.isRemovable) {
                val state = volume.state ?: Environment.MEDIA_UNKNOWN
                val isMounted = state == Environment.MEDIA_MOUNTED || state == Environment.MEDIA_MOUNTED_READ_ONLY
                val desc = volume.getDescription(context).ifBlank {
                    if (volume.uuid != null) "Removable Drive (${volume.uuid})" else "Removable Volume"
                }
                val id = volume.uuid ?: desc

                result.add(
                    UsbVolumeInfo(
                        id = id,
                        description = desc,
                        state = state,
                        isMounted = isMounted,
                        isRemovable = true,
                        uuid = volume.uuid
                    )
                )
            }
        }
        return result
    }

    /**
     * Finds the matching [StorageVolume] by its id or UUID.
     */
    fun findVolume(context: Context, volumeId: String): StorageVolume? {
        val storageManager = context.getSystemService(StorageManager::class.java) ?: return null
        return storageManager.storageVolumes.firstOrNull { vol ->
            vol.isRemovable && (vol.uuid == volumeId || vol.getDescription(context) == volumeId)
        }
    }

    /**
     * Creates an [Intent] to grant SAF access to this volume, pre-pointed at that volume on API 29+.
     */
    fun createVolumeTreeIntent(volume: StorageVolume): Intent {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            volume.createOpenDocumentTreeIntent()
        } else {
            Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
        }
    }
}
