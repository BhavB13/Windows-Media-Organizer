package com.bhavb13.dtm.organizer.ui

import android.graphics.Bitmap
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.itemsIndexed
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ElevatedCard
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Tab
import androidx.compose.material3.TabRow
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.bhavb13.dtm.organizer.core.AccessLevel
import com.bhavb13.dtm.organizer.core.BucketStorageStats
import com.bhavb13.dtm.organizer.core.ByteFormatting
import com.bhavb13.dtm.organizer.core.DuplicateDetector
import com.bhavb13.dtm.organizer.core.DuplicateGroup
import com.bhavb13.dtm.organizer.core.DuplicateScanProgress
import com.bhavb13.dtm.organizer.core.LibraryAccessOutcome
import com.bhavb13.dtm.organizer.core.MediaItem
import com.bhavb13.dtm.organizer.core.MediaKind
import java.text.DateFormat
import java.util.Date

@Composable
fun OrganizerScreen(
    state: OrganizerUiState,
    onSelectTab: (OrganizerTab) -> Unit,
    onRequestPermissions: () -> Unit,
    onOpenSafFolder: () -> Unit,
    onFilenameQueryChange: (String) -> Unit,
    onMediaKindFilterChange: (MediaKind?) -> Unit,
    onSizePresetChange: (SizeRangePreset) -> Unit,
    onDatePresetChange: (DateRangePreset) -> Unit,
    onBucketFilterChange: (String?) -> Unit,
    onClearFilters: () -> Unit,
    onLoadNextPage: () -> Unit,
    onSelectItem: (MediaItem) -> Unit,
    onDismissItemDetail: () -> Unit,
    onStartDuplicateScan: () -> Unit,
    onCancelDuplicateScan: () -> Unit,
    onGenerateReport: () -> Unit,
    loadThumbnail: suspend (MediaItem) -> Bitmap?
) {
    var showReportDialog by remember { mutableStateOf(false) }

    Scaffold(
        topBar = {
            Surface(
                tonalElevation = 2.dp,
                modifier = Modifier.fillMaxWidth()
            ) {
                Column(modifier = Modifier.padding(horizontal = 16.dp, vertical = 10.dp)) {
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        Column(modifier = Modifier.weight(1f)) {
                            Text(
                                text = "Media Organizer",
                                style = MaterialTheme.typography.titleLarge,
                                fontWeight = FontWeight.Bold
                            )
                            Text(
                                text = "Read-Only MVP • SHA-256 Content Duplicate Inspector",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            OutlinedButton(onClick = onOpenSafFolder) {
                                Text("Add Folder")
                            }
                            if (state.permissionStatus.isDegraded) {
                                Button(onClick = onRequestPermissions) {
                                    Text("Permissions")
                                }
                            }
                        }
                    }

                    Spacer(modifier = Modifier.height(6.dp))

                    // Non-negotiable read-only safety notice banner
                    Surface(
                        color = MaterialTheme.colorScheme.secondaryContainer,
                        shape = RoundedCornerShape(8.dp),
                        modifier = Modifier.fillMaxWidth()
                    ) {
                        Text(
                            text = "Read-Only Safety Contract: This app never deletes, moves, or modifies any photo or video. Duplicate review is strictly read-only and ends in a report.",
                            style = MaterialTheme.typography.labelMedium,
                            color = MaterialTheme.colorScheme.onSecondaryContainer,
                            modifier = Modifier.padding(horizontal = 10.dp, vertical = 6.dp)
                        )
                    }
                }
            }
        }
    ) { innerPadding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(innerPadding)
        ) {
            // Permission degradation banner when access is PARTIAL or DENIED
            if (state.libraryOutcome.isDegraded) {
                PermissionDegradationBanner(
                    outcome = state.libraryOutcome,
                    onRequestPermissions = onRequestPermissions,
                    onOpenSafFolder = onOpenSafFolder
                )
            }

            // Determinate progress banner when a duplicate scan is running
            if (state.isScanningDuplicates && state.scanProgress != null) {
                ScanProgressBanner(
                    progress = state.scanProgress,
                    onCancel = onCancelDuplicateScan
                )
            }

            TabRow(selectedTabIndex = state.activeTab.ordinal) {
                OrganizerTab.entries.forEach { tab ->
                    val badgeLabel = when (tab) {
                        OrganizerTab.LIBRARY -> "${state.filteredItems.size}"
                        OrganizerTab.DUPLICATES -> "${state.duplicateScanResult?.totalGroupCount ?: 0}"
                        OrganizerTab.STORAGE -> ByteFormatting.formatBytes(state.storageSummary.totalBytes)
                    }
                    Tab(
                        selected = state.activeTab == tab,
                        onClick = { onSelectTab(tab) },
                        text = { Text("${tab.title} ($badgeLabel)") }
                    )
                }
            }

            when (state.activeTab) {
                OrganizerTab.LIBRARY -> LibraryTabContent(
                    state = state,
                    onFilenameQueryChange = onFilenameQueryChange,
                    onMediaKindFilterChange = onMediaKindFilterChange,
                    onSizePresetChange = onSizePresetChange,
                    onDatePresetChange = onDatePresetChange,
                    onBucketFilterChange = onBucketFilterChange,
                    onClearFilters = onClearFilters,
                    onLoadNextPage = onLoadNextPage,
                    onSelectItem = onSelectItem,
                    onRequestPermissions = onRequestPermissions,
                    onOpenSafFolder = onOpenSafFolder,
                    loadThumbnail = loadThumbnail
                )

                OrganizerTab.DUPLICATES -> DuplicatesTabContent(
                    state = state,
                    onStartScan = onStartDuplicateScan,
                    onCancelScan = onCancelDuplicateScan,
                    onShowReport = {
                        onGenerateReport()
                        showReportDialog = true
                    },
                    onSelectItem = onSelectItem
                )

                OrganizerTab.STORAGE -> StorageTabContent(
                    state = state,
                    onStartScan = onStartDuplicateScan,
                    onSelectBucketFilter = { bucketId ->
                        onBucketFilterChange(bucketId)
                        onSelectTab(OrganizerTab.LIBRARY)
                    }
                )
            }
        }
    }

    if (state.selectedItemForDetail != null) {
        MediaItemDetailDialog(
            item = state.selectedItemForDetail,
            isLoadingMetadata = state.isLoadingDetailMetadata,
            onDismiss = onDismissItemDetail
        )
    }

    if (showReportDialog && state.duplicateReport != null) {
        DuplicateReportDialog(
            reportText = state.duplicateReport.formattedText,
            onDismiss = { showReportDialog = false }
        )
    }
}

@Composable
private fun PermissionDegradationBanner(
    outcome: LibraryAccessOutcome,
    onRequestPermissions: () -> Unit,
    onOpenSafFolder: () -> Unit
) {
    val containerColor = if (outcome.permissionStatus.accessLevel == AccessLevel.DENIED) {
        MaterialTheme.colorScheme.errorContainer
    } else {
        MaterialTheme.colorScheme.tertiaryContainer
    }
    val contentColor = if (outcome.permissionStatus.accessLevel == AccessLevel.DENIED) {
        MaterialTheme.colorScheme.onErrorContainer
    } else {
        MaterialTheme.colorScheme.onTertiaryContainer
    }

    Card(
        colors = CardDefaults.cardColors(
            containerColor = containerColor,
            contentColor = contentColor
        ),
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 8.dp)
    ) {
        Column(modifier = Modifier.padding(12.dp)) {
            Text(
                text = outcome.permissionStatus.headline,
                style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.Bold
            )
            Spacer(modifier = Modifier.height(4.dp))
            Text(
                text = outcome.explanation,
                style = MaterialTheme.typography.bodySmall
            )
            Spacer(modifier = Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = onRequestPermissions) {
                    Text(
                        if (outcome.permissionStatus.accessLevel == AccessLevel.PARTIAL) {
                            "Manage Media Selection"
                        } else {
                            "Grant Media Access"
                        }
                    )
                }
                OutlinedButton(onClick = onOpenSafFolder) {
                    Text("Browse Folder via SAF")
                }
            }
        }
    }
}

@Composable
private fun ScanProgressBanner(
    progress: DuplicateScanProgress,
    onCancel: () -> Unit
) {
    ElevatedCard(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 6.dp)
    ) {
        Column(modifier = Modifier.padding(12.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        text = progress.phase,
                        style = MaterialTheme.typography.titleSmall,
                        fontWeight = FontWeight.SemiBold
                    )
                    val detail = if (progress.currentItemName.isNotBlank()) {
                        "${progress.completedItems} / ${progress.totalItems} • ${progress.currentItemName}"
                    } else {
                        "${progress.completedItems} / ${progress.totalItems} candidates"
                    }
                    Text(
                        text = detail,
                        style = MaterialTheme.typography.bodySmall,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis
                    )
                }
                TextButton(onClick = onCancel) {
                    Text("Cancel")
                }
            }
            Spacer(modifier = Modifier.height(6.dp))
            LinearProgressIndicator(
                progress = { progress.fraction },
                modifier = Modifier.fillMaxWidth()
            )
        }
    }
}

@Composable
private fun LibraryTabContent(
    state: OrganizerUiState,
    onFilenameQueryChange: (String) -> Unit,
    onMediaKindFilterChange: (MediaKind?) -> Unit,
    onSizePresetChange: (SizeRangePreset) -> Unit,
    onDatePresetChange: (DateRangePreset) -> Unit,
    onBucketFilterChange: (String?) -> Unit,
    onClearFilters: () -> Unit,
    onLoadNextPage: () -> Unit,
    onSelectItem: (MediaItem) -> Unit,
    onRequestPermissions: () -> Unit,
    onOpenSafFolder: () -> Unit,
    loadThumbnail: suspend (MediaItem) -> Bitmap?
) {
    Column(modifier = Modifier.fillMaxSize()) {
        // Search & Filter controls
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 12.dp, vertical = 8.dp)
        ) {
            OutlinedTextField(
                value = state.filterCriteria.filenameQuery,
                onValueChange = onFilenameQueryChange,
                label = { Text("Search filename substring") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth()
            )

            Spacer(modifier = Modifier.height(6.dp))

            // Media kind + Size + Date filter chips in horizontal scroll rows
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .horizontalScroll(rememberScrollState()),
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalAlignment = Alignment.CenterVertically
            ) {
                FilterChip(
                    selected = state.filterCriteria.mediaKind == null,
                    onClick = { onMediaKindFilterChange(null) },
                    label = { Text("All Kinds") }
                )
                FilterChip(
                    selected = state.filterCriteria.mediaKind == MediaKind.IMAGE,
                    onClick = { onMediaKindFilterChange(MediaKind.IMAGE) },
                    label = { Text("Images") }
                )
                FilterChip(
                    selected = state.filterCriteria.mediaKind == MediaKind.VIDEO,
                    onClick = { onMediaKindFilterChange(MediaKind.VIDEO) },
                    label = { Text("Videos") }
                )

                SizeRangePreset.entries.forEach { preset ->
                    FilterChip(
                        selected = state.selectedSizePreset == preset,
                        onClick = { onSizePresetChange(preset) },
                        label = { Text(preset.label) }
                    )
                }

                DateRangePreset.entries.forEach { preset ->
                    FilterChip(
                        selected = state.selectedDatePreset == preset,
                        onClick = { onDatePresetChange(preset) },
                        label = { Text(preset.label) }
                    )
                }

                if (!state.filterCriteria.isDefault) {
                    TextButton(onClick = onClearFilters) {
                        Text("Reset Filters")
                    }
                }
            }

            if (state.storageSummary.byBucket.isNotEmpty()) {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .horizontalScroll(rememberScrollState()),
                    horizontalArrangement = Arrangement.spacedBy(6.dp)
                ) {
                    FilterChip(
                        selected = state.filterCriteria.bucketId == null,
                        onClick = { onBucketFilterChange(null) },
                        label = { Text("All Buckets") }
                    )
                    state.storageSummary.byBucket.forEach { bucket ->
                        FilterChip(
                            selected = state.filterCriteria.bucketId == bucket.bucketId,
                            onClick = { onBucketFilterChange(bucket.bucketId) },
                            label = { Text("${bucket.bucketName} (${bucket.count})") }
                        )
                    }
                }
            }
        }

        HorizontalDivider()

        // Main library grid or explicit empty / permission-denied state
        if (state.allItems.isEmpty() && !state.isLoadingPage) {
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(24.dp),
                contentAlignment = Alignment.Center
            ) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    val title = when (state.permissionStatus.accessLevel) {
                        AccessLevel.DENIED -> "Media Library Hidden (Permission Not Granted)"
                        AccessLevel.PARTIAL -> "No Visible Items in Current Partial Selection"
                        AccessLevel.FULL -> "No Media Found in MediaStore"
                    }
                    Text(
                        text = title,
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        text = state.libraryOutcome.explanation,
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                    Spacer(modifier = Modifier.height(16.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        if (state.permissionStatus.isDegraded) {
                            Button(onClick = onRequestPermissions) {
                                Text("Grant / Update Permissions")
                            }
                        }
                        OutlinedButton(onClick = onOpenSafFolder) {
                            Text("Select Folder via SAF")
                        }
                    }
                }
            }
        } else if (state.filteredItems.isEmpty() && !state.isLoadingPage) {
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(24.dp),
                contentAlignment = Alignment.Center
            ) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Text(
                        text = "No files match your current search or filters",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.SemiBold
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    OutlinedButton(onClick = onClearFilters) {
                        Text("Clear Search & Filters")
                    }
                }
            }
        } else {
            LazyVerticalGrid(
                columns = GridCells.Adaptive(minSize = 128.dp),
                contentPadding = PaddingValues(10.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
                modifier = Modifier.fillMaxSize()
            ) {
                itemsIndexed(
                    items = state.filteredItems,
                    key = { _, item -> item.id }
                ) { index, item ->
                    if (index >= state.filteredItems.size - 12 && state.hasMorePages && !state.isLoadingPage) {
                        LaunchedEffect(state.currentPageOffset) {
                            onLoadNextPage()
                        }
                    }
                    MediaThumbnailGridCell(
                        item = item,
                        loadThumbnail = loadThumbnail,
                        onClick = { onSelectItem(item) }
                    )
                }
            }
        }
    }
}

@Composable
private fun MediaThumbnailGridCell(
    item: MediaItem,
    loadThumbnail: suspend (MediaItem) -> Bitmap?,
    onClick: () -> Unit
) {
    val thumbnail by produceState<Bitmap?>(initialValue = null, key1 = item.id) {
        value = loadThumbnail(item)
    }

    ElevatedCard(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
    ) {
        Column {
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .aspectRatio(1f)
                    .background(MaterialTheme.colorScheme.surfaceVariant),
                contentAlignment = Alignment.Center
            ) {
                val bmp = thumbnail
                if (bmp != null) {
                    Image(
                        bitmap = bmp.asImageBitmap(),
                        contentDescription = item.displayName,
                        contentScale = ContentScale.Crop,
                        modifier = Modifier.fillMaxSize()
                    )
                } else {
                    Text(
                        text = if (item.kind == MediaKind.VIDEO) "VIDEO" else "IMAGE",
                        style = MaterialTheme.typography.labelLarge,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }

                Surface(
                    color = MaterialTheme.colorScheme.scrim.copy(alpha = 0.65f),
                    shape = RoundedCornerShape(4.dp),
                    modifier = Modifier
                        .align(Alignment.TopEnd)
                        .padding(4.dp)
                ) {
                    Text(
                        text = if (item.kind == MediaKind.VIDEO) "VID" else "IMG",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.inverseOnSurface,
                        modifier = Modifier.padding(horizontal = 4.dp, vertical = 2.dp)
                    )
                }
            }

            Column(modifier = Modifier.padding(8.dp)) {
                Text(
                    text = item.displayName,
                    style = MaterialTheme.typography.bodySmall,
                    fontWeight = FontWeight.Medium,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
                Text(
                    text = "${ByteFormatting.formatBytes(item.sizeBytes)} • ${item.bucketName}",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
            }
        }
    }
}

@Composable
private fun DuplicatesTabContent(
    state: OrganizerUiState,
    onStartScan: () -> Unit,
    onCancelScan: () -> Unit,
    onShowReport: () -> Unit,
    onSelectItem: (MediaItem) -> Unit
) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(12.dp)
    ) {
        Card(
            colors = CardDefaults.cardColors(
                containerColor = MaterialTheme.colorScheme.surfaceVariant
            ),
            modifier = Modifier.fillMaxWidth()
        ) {
            Column(modifier = Modifier.padding(12.dp)) {
                Text(
                    text = "Exact SHA-256 Duplicate Review (Read-Only)",
                    style = MaterialTheme.typography.titleMedium,
                    fontWeight = FontWeight.Bold
                )
                Spacer(modifier = Modifier.height(4.dp))
                Text(
                    text = "Files are grouped only when the SHA-256 digest of their entire byte content matches. File size is used only as a fast pre-filter; filenames and timestamps never decide duplicates. Nothing is deleted or moved in this MVP.",
                    style = MaterialTheme.typography.bodySmall
                )
                Spacer(modifier = Modifier.height(10.dp))
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Button(
                        onClick = onStartScan,
                        enabled = !state.isScanningDuplicates && state.allItems.isNotEmpty()
                    ) {
                        Text(
                            if (state.duplicateScanResult == null) {
                                "Scan Library (${state.allItems.size} items)"
                            } else {
                                "Re-run SHA-256 Scan"
                            }
                        )
                    }
                    if (state.isScanningDuplicates) {
                        OutlinedButton(onClick = onCancelScan) {
                            Text("Cancel")
                        }
                    }
                    if (state.duplicateScanResult != null && !state.duplicateScanResult.wasCancelled) {
                        OutlinedButton(onClick = onShowReport) {
                            Text("View Read-Only Report")
                        }
                    }
                }
            }
        }

        Spacer(modifier = Modifier.height(10.dp))

        val scanResult = state.duplicateScanResult
        if (scanResult == null) {
            Box(
                modifier = Modifier.fillMaxSize(),
                contentAlignment = Alignment.Center
            ) {
                Text(
                    text = "Tap 'Scan Library' above to hash same-size candidates and detect exact SHA-256 duplicates.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        } else if (scanResult.wasCancelled) {
            Box(
                modifier = Modifier.fillMaxSize(),
                contentAlignment = Alignment.Center
            ) {
                Text(
                    text = "Duplicate scan was cancelled before completion. Partial results are discarded so an incomplete scan is never mistaken for a clean library.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.error
                )
            }
        } else if (scanResult.groups.isEmpty()) {
            Box(
                modifier = Modifier.fillMaxSize(),
                contentAlignment = Alignment.Center
            ) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Text(
                        text = "No Exact SHA-256 Duplicates Found",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.SemiBold
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = "Scanned ${scanResult.scannedItemCount} files (${scanResult.hashedCandidateCount} same-size candidates hashed in full).",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant
                    )
                }
            }
        } else {
            Text(
                text = "Found ${scanResult.totalGroupCount} duplicate group(s) • ${scanResult.totalDuplicateCount} redundant copies • ${ByteFormatting.formatBytes(scanResult.totalRecoverableBytes)} recoverable",
                style = MaterialTheme.typography.labelLarge,
                fontWeight = FontWeight.SemiBold,
                modifier = Modifier.padding(vertical = 4.dp)
            )

            LazyColumn(
                verticalArrangement = Arrangement.spacedBy(10.dp),
                modifier = Modifier.fillMaxSize()
            ) {
                items(
                    items = scanResult.groups,
                    key = { it.sha256 }
                ) { group ->
                    DuplicateGroupCard(
                        group = group,
                        onSelectItem = onSelectItem
                    )
                }
            }
        }
    }
}

@Composable
private fun DuplicateGroupCard(
    group: DuplicateGroup,
    onSelectItem: (MediaItem) -> Unit
) {
    ElevatedCard(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.padding(12.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically
            ) {
                Text(
                    text = "SHA-256: ${group.sha256.take(16)}…",
                    style = MaterialTheme.typography.labelMedium,
                    fontFamily = FontFamily.Monospace
                )
                Text(
                    text = "Recoverable: ${ByteFormatting.formatBytes(group.recoverableBytes)}",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.primary,
                    fontWeight = FontWeight.Bold
                )
            }

            Spacer(modifier = Modifier.height(6.dp))

            // Recommended Keeper highlight card
            Surface(
                color = MaterialTheme.colorScheme.primaryContainer,
                shape = RoundedCornerShape(8.dp),
                modifier = Modifier
                    .fillMaxWidth()
                    .clickable { onSelectItem(group.keeper) }
            ) {
                Column(modifier = Modifier.padding(10.dp)) {
                    Text(
                        text = "RECOMMENDED KEEPER • ${group.keeper.displayName}",
                        style = MaterialTheme.typography.labelLarge,
                        fontWeight = FontWeight.Bold,
                        color = MaterialTheme.colorScheme.onPrimaryContainer
                    )
                    Text(
                        text = "${group.keeper.bucketName} • ${group.keeper.dimensionsLabel} • ${ByteFormatting.formatBytes(group.keeper.sizeBytes)}",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onPrimaryContainer
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = group.reason,
                        style = MaterialTheme.typography.bodySmall,
                        fontWeight = FontWeight.Medium,
                        color = MaterialTheme.colorScheme.onPrimaryContainer
                    )
                }
            }

            Spacer(modifier = Modifier.height(8.dp))
            Text(
                text = "Identical Redundant Copies (${group.duplicates.size}) — Read-Only (Not Deleted):",
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
            Spacer(modifier = Modifier.height(4.dp))

            group.duplicates.forEach { dup ->
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(6.dp))
                        .clickable { onSelectItem(dup) }
                        .padding(vertical = 4.dp, horizontal = 6.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column(modifier = Modifier.weight(1f)) {
                        Text(
                            text = dup.displayName,
                            style = MaterialTheme.typography.bodySmall,
                            fontWeight = FontWeight.Medium
                        )
                        Text(
                            text = "${dup.bucketName} • ${dup.dimensionsLabel}",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                    Text(
                        text = ByteFormatting.formatBytes(dup.sizeBytes),
                        style = MaterialTheme.typography.labelSmall
                    )
                }
            }
        }
    }
}

@Composable
private fun StorageTabContent(
    state: OrganizerUiState,
    onStartScan: () -> Unit,
    onSelectBucketFilter: (String) -> Unit
) {
    val summary = state.storageSummary

    LazyColumn(
        contentPadding = PaddingValues(12.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
        modifier = Modifier.fillMaxSize()
    ) {
        item {
            ElevatedCard(modifier = Modifier.fillMaxWidth()) {
                Column(modifier = Modifier.padding(14.dp)) {
                    Text(
                        text = "Library Storage Summary",
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.Bold
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    StorageMetricRow("Visible Media Files", "${summary.totalCount} items")
                    StorageMetricRow("Total Visible Size", "${ByteFormatting.formatBytes(summary.totalBytes)} (${summary.totalBytes} B)")
                    StorageMetricRow(
                        label = "Recoverable via Exact Duplicates",
                        value = if (state.duplicateScanResult == null) {
                            "Run SHA-256 scan to calculate"
                        } else {
                            "${ByteFormatting.formatBytes(summary.recoverableBytes)} (${summary.duplicateItemCount} redundant files in ${summary.duplicateGroupCount} groups)"
                        }
                    )
                    StorageMetricRow(
                        label = "Net Size After Deduplication",
                        value = ByteFormatting.formatBytes(summary.netBytesAfterDeduplication)
                    )
                    if (state.duplicateScanResult == null && state.allItems.isNotEmpty()) {
                        Spacer(modifier = Modifier.height(8.dp))
                        OutlinedButton(onClick = onStartScan) {
                            Text("Calculate Recoverable Duplicate Bytes (SHA-256)")
                        }
                    }
                }
            }
        }

        item {
            ElevatedCard(modifier = Modifier.fillMaxWidth()) {
                Column(modifier = Modifier.padding(14.dp)) {
                    Text(
                        text = "By Media Kind",
                        style = MaterialTheme.typography.titleSmall,
                        fontWeight = FontWeight.Bold
                    )
                    Spacer(modifier = Modifier.height(6.dp))
                    val img = summary.imageStats
                    val vid = summary.videoStats
                    StorageMetricRow(
                        label = "Images",
                        value = "${img.count} files • ${ByteFormatting.formatBytes(img.bytes)}"
                    )
                    StorageMetricRow(
                        label = "Videos",
                        value = "${vid.count} files • ${ByteFormatting.formatBytes(vid.bytes)}"
                    )
                }
            }
        }

        item {
            Text(
                text = "By Containing Bucket (${summary.byBucket.size})",
                style = MaterialTheme.typography.titleSmall,
                fontWeight = FontWeight.Bold,
                modifier = Modifier.padding(top = 4.dp)
            )
        }

        items(
            items = summary.byBucket,
            key = { it.bucketId }
        ) { bucket: BucketStorageStats ->
            Card(
                modifier = Modifier
                    .fillMaxWidth()
                    .clickable { onSelectBucketFilter(bucket.bucketId) }
            ) {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(12.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically
                ) {
                    Column(modifier = Modifier.weight(1f)) {
                        Text(
                            text = bucket.bucketName,
                            style = MaterialTheme.typography.bodyMedium,
                            fontWeight = FontWeight.SemiBold
                        )
                        Text(
                            text = "${bucket.count} items (${bucket.imageCount} images, ${bucket.videoCount} videos)",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant
                        )
                    }
                    Text(
                        text = ByteFormatting.formatBytes(bucket.bytes),
                        style = MaterialTheme.typography.bodyMedium,
                        fontWeight = FontWeight.Bold
                    )
                }
            }
        }
    }
}

@Composable
private fun StorageMetricRow(label: String, value: String) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 3.dp),
        horizontalArrangement = Arrangement.SpaceBetween
    ) {
        Text(
            text = label,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        Spacer(modifier = Modifier.width(12.dp))
        Text(
            text = value,
            style = MaterialTheme.typography.bodySmall,
            fontWeight = FontWeight.Medium
        )
    }
}

@Composable
private fun MediaItemDetailDialog(
    item: MediaItem,
    isLoadingMetadata: Boolean,
    onDismiss: () -> Unit
) {
    val formattedDate = remember(item.effectiveCaptureDateMillis) {
        if (item.effectiveCaptureDateMillis > 0L) {
            DateFormat.getDateTimeInstance(DateFormat.MEDIUM, DateFormat.SHORT)
                .format(Date(item.effectiveCaptureDateMillis))
        } else {
            "Unknown date"
        }
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = {
            Text(
                text = item.displayName,
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold
            )
        },
        text = {
            Column(
                modifier = Modifier.verticalScroll(rememberScrollState()),
                verticalArrangement = Arrangement.spacedBy(6.dp)
            ) {
                DetailField("Media Kind", "${item.kind.displayName} (${item.mimeType})")
                DetailField("Dimensions", item.dimensionsLabel)
                DetailField("File Size", "${ByteFormatting.formatBytes(item.sizeBytes)} (${item.sizeBytes} bytes)")
                DetailField("Capture / Modified Date", formattedDate)
                DetailField(
                    label = "Camera Make / Model",
                    value = when {
                        isLoadingMetadata -> "Reading EXIF metadata…"
                        item.cameraSummary != null -> item.cameraSummary!!
                        else -> "Not exposed in file metadata"
                    }
                )
                DetailField("Containing Bucket", item.bucketName)
                if (!item.relativePath.isNullOrBlank()) {
                    DetailField("Relative Path", item.relativePath)
                }
            }
        },
        confirmButton = {
            TextButton(onClick = onDismiss) {
                Text("Close")
            }
        }
    )
}

@Composable
private fun DetailField(label: String, value: String) {
    Column {
        Text(
            text = label,
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
        Text(
            text = value,
            style = MaterialTheme.typography.bodyMedium,
            fontWeight = FontWeight.Medium
        )
    }
}

@Composable
private fun DuplicateReportDialog(
    reportText: String,
    onDismiss: () -> Unit
) {
    val clipboard = LocalClipboardManager.current

    AlertDialog(
        onDismissRequest = onDismiss,
        title = {
            Text(
                text = "Read-Only Duplicate Review Report",
                style = MaterialTheme.typography.titleMedium,
                fontWeight = FontWeight.Bold
            )
        },
        text = {
            Column(modifier = Modifier.verticalScroll(rememberScrollState())) {
                Text(
                    text = DuplicateDetector.READ_ONLY_SAFETY_NOTICE,
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.primary,
                    fontWeight = FontWeight.SemiBold
                )
                Spacer(modifier = Modifier.height(8.dp))
                Text(
                    text = reportText,
                    style = MaterialTheme.typography.bodySmall,
                    fontFamily = FontFamily.Monospace
                )
            }
        },
        confirmButton = {
            Button(
                onClick = {
                    clipboard.setText(AnnotatedString(reportText))
                }
            ) {
                Text("Copy Report")
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text("Close")
            }
        }
    )
}
