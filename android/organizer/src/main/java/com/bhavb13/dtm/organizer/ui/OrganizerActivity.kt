package com.bhavb13.dtm.organizer.ui

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import com.bhavb13.dtm.organizer.data.AndroidPermissionHelper

/**
 * Main entry-point Activity for the Read-Only Jetpack Compose Media Organizer MVP.
 */
class OrganizerActivity : ComponentActivity() {

    private val viewModel: OrganizerViewModel by viewModels()

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) {
        viewModel.refreshPermissionsAndReload()
    }

    private val safTreeLauncher = registerForActivityResult(
        ActivityResultContracts.OpenDocumentTree()
    ) { treeUri ->
        if (treeUri != null) {
            try {
                // Take only READ permission; never request or persist write permission.
                contentResolver.takePersistableUriPermission(
                    treeUri,
                    Intent.FLAG_GRANT_READ_URI_PERMISSION
                )
            } catch (_: SecurityException) {
                // Non-persistable grant is still usable for the current session.
            }
            viewModel.addSafFolderReadOnly(treeUri)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                val state by viewModel.uiState.collectAsState()
                OrganizerScreen(
                    state = state,
                    onSelectTab = viewModel::selectTab,
                    onRequestPermissions = {
                        permissionLauncher.launch(AndroidPermissionHelper.permissionsToRequest())
                    },
                    onOpenSafFolder = {
                        safTreeLauncher.launch(null)
                    },
                    onFilenameQueryChange = viewModel::updateFilenameQuery,
                    onMediaKindFilterChange = viewModel::updateMediaKindFilter,
                    onSizePresetChange = viewModel::updateSizePreset,
                    onDatePresetChange = viewModel::updateDatePreset,
                    onBucketFilterChange = viewModel::updateBucketFilter,
                    onClearFilters = viewModel::clearFilters,
                    onLoadNextPage = viewModel::loadNextPage,
                    onSelectItem = viewModel::inspectItemDetail,
                    onDismissItemDetail = viewModel::dismissItemDetail,
                    onStartDuplicateScan = viewModel::startDuplicateScan,
                    onCancelDuplicateScan = viewModel::cancelDuplicateScan,
                    onGenerateReport = viewModel::generateReadOnlyReport,
                    loadThumbnail = viewModel::loadThumbnail
                )
            }
        }
    }

    override fun onResume() {
        super.onResume()
        // Re-check permissions when returning from system Settings.
        val latest = AndroidPermissionHelper.checkPermissionStatus(this)
        if (latest != viewModel.uiState.value.permissionStatus) {
            viewModel.refreshPermissionsAndReload()
        }
    }
}
