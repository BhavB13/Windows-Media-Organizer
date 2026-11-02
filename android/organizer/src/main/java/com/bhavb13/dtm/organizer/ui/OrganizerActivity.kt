package com.bhavb13.dtm.organizer.ui

import android.app.Activity
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
import com.bhavb13.dtm.organizer.data.UsbStorageHelper

/**
 * Main entry-point Activity for the Jetpack Compose Media Organizer.
 */
class OrganizerActivity : ComponentActivity() {

    private val viewModel: OrganizerViewModel by viewModels()

    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) {
        viewModel.refreshPermissionsAndReload()
    }

    private val safSourceFolderLauncher = registerForActivityResult(
        ActivityResultContracts.OpenDocumentTree()
    ) { treeUri ->
        if (treeUri != null) {
            viewModel.addGrantedSourceFolder(treeUri)
        }
    }

    private val safDestFolderLauncher = registerForActivityResult(
        ActivityResultContracts.OpenDocumentTree()
    ) { treeUri ->
        if (treeUri != null) {
            viewModel.setOrganizeDestination(treeUri)
        }
    }

    private val volumeTreeLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            val treeUri = result.data?.data
            if (treeUri != null) {
                viewModel.addGrantedSourceFolder(treeUri)
            }
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
                        safSourceFolderLauncher.launch(null)
                    },
                    onRemoveGrantedFolder = viewModel::removeGrantedFolder,
                    onGrantVolumeAccess = { volumeInfo ->
                        val volume = UsbStorageHelper.findVolume(this, volumeInfo.id)
                        if (volume != null) {
                            val intent = UsbStorageHelper.createVolumeTreeIntent(volume)
                            volumeTreeLauncher.launch(intent)
                        } else {
                            safSourceFolderLauncher.launch(null)
                        }
                    },
                    onSelectDestinationFolder = {
                        safDestFolderLauncher.launch(null)
                    },
                    onSelectOrganizeTemplate = viewModel::selectOrganizeTemplate,
                    onCreateOrganizePlan = viewModel::createOrganizePlan,
                    onClearOrganizePlan = viewModel::clearOrganizePlan,
                    onExecuteOrganizePlan = viewModel::executeOrganizePlan,
                    onCancelOrganize = viewModel::cancelOrganize,
                    onDismissOrganizeResult = viewModel::dismissOrganizeResult,
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
        viewModel.refreshDrivesAndGrantedFolders()
        val latest = AndroidPermissionHelper.checkPermissionStatus(this)
        if (latest != viewModel.uiState.value.permissionStatus) {
            viewModel.refreshPermissionsAndReload()
        }
    }
}
