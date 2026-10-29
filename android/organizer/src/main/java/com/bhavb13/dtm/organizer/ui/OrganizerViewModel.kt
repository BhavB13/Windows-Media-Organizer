package com.bhavb13.dtm.organizer.ui

import android.app.Application
import android.graphics.Bitmap
import android.net.Uri
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.bhavb13.dtm.organizer.core.AccessLevel
import com.bhavb13.dtm.organizer.core.DuplicateDetector
import com.bhavb13.dtm.organizer.core.DuplicateReport
import com.bhavb13.dtm.organizer.core.DuplicateScanProgress
import com.bhavb13.dtm.organizer.core.DuplicateScanResult
import com.bhavb13.dtm.organizer.core.LibraryAccessOutcome
import com.bhavb13.dtm.organizer.core.MediaFilter
import com.bhavb13.dtm.organizer.core.MediaFilterCriteria
import com.bhavb13.dtm.organizer.core.MediaItem
import com.bhavb13.dtm.organizer.core.MediaKind
import com.bhavb13.dtm.organizer.core.PermissionEvaluator
import com.bhavb13.dtm.organizer.core.PermissionStatus
import com.bhavb13.dtm.organizer.core.StorageAggregator
import com.bhavb13.dtm.organizer.core.StorageSummary
import com.bhavb13.dtm.organizer.data.AndroidPermissionHelper
import com.bhavb13.dtm.organizer.data.MediaStoreRepository
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
    STORAGE("Storage")
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
    val storageSummary: StorageSummary = StorageAggregator.summarize(emptyList())
)

class OrganizerViewModel(application: Application) : AndroidViewModel(application) {

    companion object {
        private const val PAGE_SIZE = 60
        private const val MILLIS_PER_DAY = 24L * 60L * 60L * 1000L
    }

    private val repository = MediaStoreRepository(application.applicationContext)

    private val mediaStoreItems = mutableListOf<MediaItem>()
    private val safItems = mutableListOf<MediaItem>()
    private val enrichedItemsById = mutableMapOf<String, MediaItem>()

    private var duplicateScanJob: Job? = null
    private var pageLoadJob: Job? = null

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
        refreshPermissionsAndReload()
    }

    val uiState: StateFlow<OrganizerUiState> = _uiState.asStateFlow()

    fun selectTab(tab: OrganizerTab) {
        _uiState.update { it.copy(activeTab = tab) }
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

    fun addSafFolderReadOnly(treeUri: Uri) {
        viewModelScope.launch {
            _uiState.update { it.copy(isLoadingPage = true) }
            val scanned = repository.scanSafFolderTreeReadOnly(treeUri)
            val existingIds = safItems.mapTo(mutableSetOf()) { it.id }
            for (item in scanned) {
                if (item.id !in existingIds) {
                    safItems.add(item)
                }
            }
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
