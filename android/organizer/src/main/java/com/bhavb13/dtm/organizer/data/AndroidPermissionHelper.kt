package com.bhavb13.dtm.organizer.data

import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.ContextCompat
import com.bhavb13.dtm.organizer.core.MediaPermissions
import com.bhavb13.dtm.organizer.core.PermissionEvaluator
import com.bhavb13.dtm.organizer.core.PermissionStatus

/**
 * Bridges Android's runtime permission checks to the pure-Kotlin [PermissionEvaluator].
 */
object AndroidPermissionHelper {

    fun checkPermissionStatus(context: Context): PermissionStatus {
        val sdkInt = Build.VERSION.SDK_INT
        val candidatePermissions = listOf(
            MediaPermissions.READ_EXTERNAL_STORAGE,
            MediaPermissions.READ_MEDIA_IMAGES,
            MediaPermissions.READ_MEDIA_VIDEO,
            MediaPermissions.READ_MEDIA_VISUAL_USER_SELECTED
        )
        val granted = candidatePermissions.filterTo(mutableSetOf()) { perm ->
            ContextCompat.checkSelfPermission(context, perm) == PackageManager.PERMISSION_GRANTED
        }
        return PermissionEvaluator.evaluate(sdkInt = sdkInt, grantedPermissions = granted)
    }

    fun permissionsToRequest(): Array<String> {
        return MediaPermissions.requiredPermissionsForSdk(Build.VERSION.SDK_INT).toTypedArray()
    }
}
