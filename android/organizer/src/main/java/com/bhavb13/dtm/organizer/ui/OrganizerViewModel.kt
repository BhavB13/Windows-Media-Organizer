package com.bhavb13.dtm.organizer.ui

import android.app.Application
import android.graphics.Bitmap
import android.net.Uri
import android.provider.DocumentsContract
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.bhavb13.dtm.organizer.core.AccessLevel
import com.bhavb13.dtm.organizer.core.DuplicateDetector
import com.bhavb13.dtm.organizer.core.DuplicateReport
import com.bhavb13.dtm.organizer.core.DuplicateScanProgress
import com.bhavb13.dtm.organizer.core.DuplicateScanResult
import com.bhavb13.dtm.organizer.core.ExistingDestinationFile
import com.bhavb13.dtm.organizer.core.GrantedFolderInfo
import com.bhavb13.dtm.organizer.core.LibraryAccessOutcome
import com.bhavb13.dtm.organizer.core.MediaFilter
import com.bhavb13.dtm.organizer.core.MediaFilterCriteria
import com.bhavb13.dtm.organizer.core.MediaItem
import com.bhavb13.dtm.organizer.core.MediaKind
import com.bhavb13.dtm.organizer.core.OrganizePlan
import com.bhavb13.dtm.organizer.core.OrganizePlanner
import com.bhavb13.dtm.organizer.core.OrganizeProgress
import com.bhavb13.dtm.organizer.core.OrganizeResult
import com.bhavb13.dtm.organizer.core.OrganizeTemplate
import com.bhavb13.dtm.organizer.core.PermissionEvaluator
import com.bhavb13.dtm.organizer.core.PermissionStatus
import com.bhavb13.dtm.organizer.core.Sha256Hasher
import com.bhavb13.dtm.organizer.core.StorageAggregator
import com.bhavb13.dtm.organizer.core.StorageSummary
import com.bhavb13.dtm.organizer.core.UsbVolumeInfo
import com.bhavb13.dtm.organizer.data.AndroidPermissionHelper
import com.bhavb13.dtm.organizer.data.MediaStoreRepository
import com.bhavb13.dtm.organizer.data.SafFolderManager
import com.bhavb13.dtm.organizer.data.SafeCopyExecutor
import com.bhavb13.dtm.organizer.data.UsbStorageHelper
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

enum class OrganizerTab(val title: String) {
    LIBRARY("Library"),
    DUPLICATES("Duplicates"),
    ORGANIZE("Organize"),
    STORAGE("Storage"),
    FOLDERS("Folders & USB")
}

enum class SizeRangePreset(val label: String, val minBytes: Long?, val maxBytes: Long?) {
    ANY("Any size", null, null),
    UNDER_1_MB("< 1 MB", null, 1_048_576L),
    ONE_TO_10_MB("1–10 MB", 1_048_576L, 10_485_760L),
    OVER_10_MB("> 10 MB", 10_485_760L, null)
}

enum class DateRangePreset(val label: String, val lookbackDays: Long?) {
    ANY("Any date", null),
    LAST_7_DAYS("Last 7 days", 7L),
    LAST_30_DAYS("Last 30 days", 30L),
    LAST_365_DAYS("Last year", 365L)
}

data class OrganizerUiState(
    val activeTab: OrganizerTab = OrganizerTab.LIBRARY,
    val permissionStatus: PermissionStatus,
    val libraryOutcome: LibraryAccessOutcome,
    val allItems: List<MediaItem> = emptyList(),
    val filteredItems: List<MediaItem> = emptyList(),
    val filterCriteria: MediaFilterCriteria = MediaFilterCriteria(),
    val selectedSizePreset: SizeRangePreset = SizeRangePreset.ANY,
    val selectedDatePreset: DateRangePreset = DateRangePreset.ANY,
    val isLoadingPage: Boolean = false,
    val hasMorePages: Boolean = false,
    val currentPageOffset: Int = 0,
    val selectedItemForDetail: MediaItem? = null,
    val isLoadingDetailMetadata: Boolean = false,
    val isScanningDuplicates: Boolean = false,
    val scanProgress: DuplicateScanProgress? = null,
    val duplicateScanResult: DuplicateScanResult? = null,
    val duplicateReport: DuplicateReport? = null,
    val storageSummary: StorageSummary = StorageAggregator.summarize(emptyList()),
    // SAF Folders & USB OTG
    val grantedFolders: List<GrantedFolderInfo> = emptyList(),
    val removableVolumes: List<UsbVolumeInfo> = emptyList(),
    // Safe Organize
    val destinationTreeUri: String? = null,
    val destinationLabel: String? = null,
    val selectedOrganizeTemplate: OrganizeTemplate = OrganizeTemplate.YEAR_MONTH,
    val isPlanningOrganize: Boolean = false,
    val organizePlan: OrganizePlan? = null,
    val isExecutingOrganize: Boolean = false,
    val organizeProgress: OrganizeProgress? = null,
    val organizeResult: OrganizeResult? = null
)

class OrganizerViewModel(application: Application) : AndroidViewModel(application) {

    companion object {
        private const val PAGE_SIZE = 60
        private const val MILLIS_PER_DAY = 24L * 60L * 60L * 1000L
    }

    private val repository = MediaStoreRepository(application.applicationContext)
    private val safFolderManager = SafFolderManager(application.applicationContext)
    private val safeCopyExecutor = SafeCopyExecutor(application.applicationContext)

    private val mediaStoreItems = mutableListOf<MediaItem>()
    private val safItems = mutableListOf<MediaItem>()
    private val enrichedItemsById = mutableMapOf<String, MediaItem>()

    private var duplicateScanJob: Job? = null
    private var pageLoadJob: Job? = null
    private var organizeJob: Job? = null

    private val _uiState: MutableStateFlow<OrganizerUiState>

    init {
        val initialPerm = AndroidPermissionHelper.checkPermissionStatus(application.applicationContext)
        val initialOutcome = PermissionEvaluator.resolveLibraryOutcome(emptyList(), initialPerm)
        _uiState = MutableStateFlow(
            OrganizerUiState(
                permissionStatus = initialPerm,
                libraryOutcome = initialOutcome
            )
        )
        refreshDrivesAndGrantedFolders()
        refreshPermissionsAndReload()
        loadPersistedFolderItems()
    }

    val uiState: StateFlow<OrganizerUiState> = _uiState.asStateFlow()

    fun selectTab(tab: OrganizerTab) {
        _uiState.update { it.copy(activeTab = tab) }
    }

    fun refreshDrivesAndGrantedFolders() {
        val volumes = UsbStorageHelper.getRemovableVolumes(getApplication())
        val grants = safFolderManager.listPersistedFolderGrants()
        _uiState.update {
            it.copy(
                removableVolumes = volumes,
                grantedFolders = grants
            )
        }
    }

    private fun loadPersistedFolderItems() {
        val grants = safFolderManager.listPersistedFolderGrants()
        if (grants.isEmpty()) return

        viewModelScope.launch {
            for (grant in grants) {
                try {
                    val uri = Uri.parse(grant.treeUri)
                    val scanned = safFolderManager.browseTreeFiles(uri)
                    val existingIds = safItems.mapTo(mutableSetOf()) { it.id }
                    for (item in scanned) {
                        if (item.id !in existingIds) {
                            safItems.add(item)
                        }
                    }
                } catch (_: Exception) {
                    // Ignore transient load failure on startup
                }
            }
            val combined = mergeItems()
            _uiState.update { state ->
                val outcome = PermissionEvaluator.resolveLibraryOutcome(combined, state.permissionStatus)
                state.copy(
                    allItems = combined,
                    filteredItems = MediaFilter.filter(combined, state.filterCriteria),
                    libraryOutcome = outcome,
                    storageSummary = StorageAggregator.summarize(
                        combined,
                        state.duplicateScanResult?.groups ?: emptyList()
                    )
                )
            }
        }
    }

    fun refreshPermissionsAndReload() {
        val perm = AndroidPermissionHelper.checkPermissionStatus(getApplication())
        pageLoadJob?.cancel()
        mediaStoreItems.clear()

        _uiState.update { state ->
            val combined = mergeItems()
            val outcome = PermissionEvaluator.resolveLibraryOutcome(combined, perm)
            state.copy(
                permissionStatus = perm,
                libraryOutcome = outcome,
                allItems = combined,
                filteredItems = MediaFilter.filter(combined, state.filterCriteria),
                currentPageOffset = 0,
                hasMorePages = perm.accessLevel != AccessLevel.DENIED,
                isLoadingPage = false,
                storageSummary = StorageAggregator.summarize(
                    combined,
                    state.duplicateScanResult?.groups ?: emptyList()
                )
            )
        }

        if (perm.canReadImages || perm.canReadVideos) {
            loadNextPage()
        }
    }

    fun loadNextPage() {
        val snapshot = _uiState.value
        if (snapshot.isLoadingPage || !snapshot.hasMorePages) return
        val perm = snapshot.permissionStatus
        if (!perm.canReadImages && !perm.canReadVideos) return

        val offset = snapshot.currentPageOffset
        _uiState.update { it.copy(isLoadingPage = true) }

        pageLoadJob = viewModelScope.launch {
            val page = repository.queryMediaStorePage(
                permissionStatus = perm,
                offset = offset,
                limit = PAGE_SIZE
            )
            mediaStoreItems.addAll(page)
            val combined = mergeItems()
            val reachedEnd = page.size < PAGE_SIZE

            _uiState.update { state ->
                val outcome = PermissionEvaluator.resolveLibraryOutcome(combined, state.permissionStatus)
                state.copy(
                    allItems = combined,
                    filteredItems = MediaFilter.filter(combined, state.filterCriteria),
                    libraryOutcome = outcome,
                    isLoadingPage = false,
                    hasMorePages = !reachedEnd,
                    currentPageOffset = offset + page.size,
                    storageSummary = StorageAggregator.summarize(
                        combined,
                        state.duplicateScanResult?.groups ?: emptyList()
                    )
                )
            }
        }
    }

    fun addGrantedSourceFolder(treeUri: Uri) {
        safFolderManager.takePersistableReadPermission(treeUri)
        viewModelScope.launch {
            _uiState.update { it.copy(isLoadingPage = true) }
            val scanned = safFolderManager.browseTreeFiles(treeUri)
            val existingIds = safItems.mapTo(mutableSetOf()) { it.id }
            for (item in scanned) {
                if (item.id !in existingIds) {
                    safItems.add(item)
                }
            }
            refreshDrivesAndGrantedFolders()
            val combined = mergeItems()
            _uiState.update { state ->
                val outcome = PermissionEvaluator.resolveLibraryOutcome(combined, state.permissionStatus)
                state.copy(
                    allItems = combined,
                    filteredItems = MediaFilter.filter(combined, state.filterCriteria),
                    libraryOutcome = outcome,
                    isLoadingPage = false,
                    storageSummary = StorageAggregator.summarize(
                        combined,
                        state.duplicateScanResult?.groups ?: emptyList()
                    )
                )
            }
        }
    }

    fun addSafFolderReadOnly(treeUri: Uri) {
        addGrantedSourceFolder(treeUri)
    }

    fun removeGrantedFolder(folderUriString: String) {
        val uri = Uri.parse(folderUriString)
        safFolderManager.releaseReadPermission(uri)
        val folderLabel = safFolderManager.resolveFolderDisplayName(uri)
        val bucketTarget = "SAF: $folderLabel"
        safItems.removeAll { it.bucketName == bucketTarget || it.id.startsWith(folderUriString) }

        refreshDrivesAndGrantedFolders()
        val combined = mergeItems()
        _uiState.update { state ->
            val outcome = PermissionEvaluator.resolveLibraryOutcome(combined, state.permissionStatus)
            state.copy(
                allItems = combined,
                filteredItems = MediaFilter.filter(combined, state.filterCriteria),
                libraryOutcome = outcome,
                storageSummary = StorageAggregator.summarize(
                    combined,
                    state.duplicateScanResult?.groups ?: emptyList()
                )
            )
        }
    }

    fun setOrganizeDestination(treeUri: Uri) {
        safFolderManager.takePersistableReadWritePermission(treeUri)
        val label = safFolderManager.resolveFolderDisplayName(treeUri)
        _uiState.update {
            it.copy(
                destinationTreeUri = treeUri.toString(),
                destinationLabel = label,
                organizePlan = null,
                organizeResult = null
            )
        }
    }

    fun selectOrganizeTemplate(template: OrganizeTemplate) {
        _uiState.update {
            it.copy(
                selectedOrganizeTemplate = template,
                organizePlan = null,
                organizeResult = null
            )
        }
    }

    fun createOrganizePlan() {
        val destUriStr = _uiState.value.destinationTreeUri ?: return
        val destLabel = _uiState.value.destinationLabel ?: "Destination"
        val template = _uiState.value.selectedOrganizeTemplate
        val itemsToOrganize = if (_uiState.value.filterCriteria.isDefault) {
            _uiState.value.allItems
        } else {
            _uiState.value.filteredItems
        }
        if (itemsToOrganize.isEmpty()) return

        _uiState.update { it.copy(isPlanningOrganize = true, organizePlan = null, organizeResult = null) }

        viewModelScope.launch(Dispatchers.IO) {
            val destUri = Uri.parse(destUriStr)
            val subfolders = itemsToOrganize.map {
                template.render(it.captureDateMillis, it.dateModifiedMillis)
            }.distinct()

            val existingFilesBySubfolder = mutableMapOf<String, List<ExistingDestinationFile>>()
            for (sub in subfolders) {
                val existing = safeCopyExecutor.listExistingFilesInSubfolder(destUri, sub)
                existingFilesBySubfolder[sub] = existing
            }

            val plan = OrganizePlanner.plan(
                items = itemsToOrganize,
                template = template,
                destinationTreeUri = destUriStr,
                destinationLabel = destLabel,
                destinationFiles = { subfolder, fileName ->
                    existingFilesBySubfolder[subfolder]?.firstOrNull { it.fileName.equals(fileName, ignoreCase = true) }
                },
                sourceSha256Provider = { item ->
                    try {
                        repository.openStream(item).use { Sha256Hasher.hashStream(it) }
                    } catch (_: Exception) {
                        null
                    }
                },
                destSha256Provider = { existing ->
                    existing.documentUri?.let { safeCopyExecutor.hashExistingDocument(Uri.parse(it)) }
                }
            )

            _uiState.update { it.copy(isPlanningOrganize = false, organizePlan = plan) }
        }
    }

    fun clearOrganizePlan() {
        _uiState.update { it.copy(organizePlan = null, organizeResult = null) }
    }

    fun executeOrganizePlan() {
        val plan = _uiState.value.organizePlan ?: return
        if (_uiState.value.isExecutingOrganize) return

        _uiState.update {
            it.copy(
                isExecutingOrganize = true,
                organizeProgress = OrganizeProgress(
                    completedCount = 0,
                    totalCount = plan.items.size,
                    currentFileName = "",
                    bytesCopied = 0L,
                    totalBytesToCopy = plan.totalCopyBytes,
                    phase = "Starting safe organize copy..."
                ),
                organizeResult = null
            )
        }

        organizeJob = viewModelScope.launch {
            val result = safeCopyExecutor.execute(
                plan = plan,
                onProgress = { prog ->
                    _uiState.update { it.copy(organizeProgress = prog) }
                },
                isCancelled = { !coroutineContext.isActive }
            )
            _uiState.update {
                it.copy(
                    isExecutingOrganize = false,
                    organizeProgress = null,
                    organizeResult = result
                )
            }
        }
    }

    fun cancelOrganize() {
        organizeJob?.cancel()
        organizeJob = null
        _uiState.update {
            it.copy(
                isExecutingOrganize = false,
                organizeProgress = null
            )
        }
    }

    fun dismissOrganizeResult() {
        _uiState.update { it.copy(organizeResult = null) }
    }

    fun updateFilenameQuery(query: String) {
        updateCriteria { it.copy(filenameQuery = query) }
    }

    fun updateMediaKindFilter(kind: MediaKind?) {
        updateCriteria { it.copy(mediaKind = kind) }
    }

    fun updateBucketFilter(bucketId: String?) {
        updateCriteria { it.copy(bucketId = bucketId) }
    }

    fun updateSizePreset(preset: SizeRangePreset) {
        _uiState.update { state ->
            val updatedCriteria = state.filterCriteria.copy(
                minSizeBytes = preset.minBytes,
                maxSizeBytes = preset.maxBytes
            )
            state.copy(
                selectedSizePreset = preset,
                filterCriteria = updatedCriteria,
                filteredItems = MediaFilter.filter(state.allItems, updatedCriteria)
            )
        }
    }

    fun updateDatePreset(preset: DateRangePreset) {
        val now = System.currentTimeMillis()
        val minMillis = preset.lookbackDays?.let { days -> now - days * MILLIS_PER_DAY }
        _uiState.update { state ->
            val updatedCriteria = state.filterCriteria.copy(
                minDateMillis = minMillis,
                maxDateMillis = null
            )
            state.copy(
                selectedDatePreset = preset,
                filterCriteria = updatedCriteria,
                filteredItems = MediaFilter.filter(state.allItems, updatedCriteria)
            )
        }
    }

    fun clearFilters() {
        _uiState.update { state ->
            val cleared = MediaFilterCriteria()
            state.copy(
                filterCriteria = cleared,
                selectedSizePreset = SizeRangePreset.ANY,
                selectedDatePreset = DateRangePreset.ANY,
                filteredItems = state.allItems
            )
        }
    }

    private fun updateCriteria(transform: (MediaFilterCriteria) -> MediaFilterCriteria) {
        _uiState.update { state ->
            val nextCriteria = transform(state.filterCriteria)
            state.copy(
                filterCriteria = nextCriteria,
                filteredItems = MediaFilter.filter(state.allItems, nextCriteria)
            )
        }
    }

    fun inspectItemDetail(item: MediaItem) {
        val cached = enrichedItemsById[item.id] ?: item
        _uiState.update {
            it.copy(
                selectedItemForDetail = cached,
                isLoadingDetailMetadata = cached.cameraSummary == null && cached.kind == MediaKind.IMAGE
            )
        }
        if (cached.kind == MediaKind.IMAGE && cached.cameraSummary == null) {
            viewModelScope.launch {
                val enriched = repository.enrichItemMetadata(cached)
                enrichedItemsById[enriched.id] = enriched
                _uiState.update { state ->
                    if (state.selectedItemForDetail?.id == enriched.id) {
                        state.copy(
                            selectedItemForDetail = enriched,
                            isLoadingDetailMetadata = false
                        )
                    } else {
                        state
                    }
                }
            }
        }
    }

    fun dismissItemDetail() {
        _uiState.update {
            it.copy(
                selectedItemForDetail = null,
                isLoadingDetailMetadata = false
            )
        }
    }

    fun startDuplicateScan() {
        if (_uiState.value.isScanningDuplicates) return
        val itemsToScan = _uiState.value.allItems
        _uiState.update {
            it.copy(
                isScanningDuplicates = true,
                scanProgress = DuplicateScanProgress(
                    phase = "Preparing SHA-256 scan",
                    completedItems = 0,
                    totalItems = itemsToScan.size,
                    currentItemName = ""
                ),
                duplicateReport = null
            )
        }

        duplicateScanJob = viewModelScope.launch {
            try {
                val result = withContext(Dispatchers.IO) {
                    DuplicateDetector.findDuplicates(
                        items = itemsToScan,
                        source = repository,
                        onProgress = { progress ->
                            _uiState.update { state ->
                                state.copy(scanProgress = progress)
                            }
                        },
                        isCancelled = { !coroutineContext.isActive }
                    )
                }
                val report = if (!result.wasCancelled) {
                    DuplicateDetector.buildReadOnlyReport(result)
                } else {
                    null
                }
                _uiState.update { state ->
                    state.copy(
                        isScanningDuplicates = false,
                        scanProgress = null,
                        duplicateScanResult = result,
                        duplicateReport = report,
                        storageSummary = StorageAggregator.summarize(
                            state.allItems,
                            result.groups
                        )
                    )
                }
            } catch (_: CancellationException) {
                _uiState.update { state ->
                    state.copy(
                        isScanningDuplicates = false,
                        scanProgress = null,
                        duplicateScanResult = DuplicateScanResult.cancelled(itemsToScan.size)
                    )
                }
            }
        }
    }

    fun cancelDuplicateScan() {
        duplicateScanJob?.cancel()
        duplicateScanJob = null
        _uiState.update { state ->
            state.copy(
                isScanningDuplicates = false,
                scanProgress = null,
                duplicateScanResult = DuplicateScanResult.cancelled(state.allItems.size)
            )
        }
    }

    fun generateReadOnlyReport() {
        val result = _uiState.value.duplicateScanResult ?: return
        val report = DuplicateDetector.buildReadOnlyReport(result)
        _uiState.update { it.copy(duplicateReport = report) }
    }

    suspend fun loadThumbnail(item: MediaItem): Bitmap? {
        return repository.loadThumbnail(item)
    }

    private fun mergeItems(): List<MediaItem> {
        val byId = linkedMapOf<String, MediaItem>()
        for (item in mediaStoreItems) {
            byId[item.id] = enrichedItemsById[item.id] ?: item
        }
        for (item in safItems) {
            byId.putIfAbsent(item.id, enrichedItemsById[item.id] ?: item)
        }
        return byId.values.toList()
    }
}
