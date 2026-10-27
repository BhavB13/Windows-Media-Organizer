"""PySide6 Library Catalog page for incremental indexing, storage analytics, and paged search."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QThreadPool, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..controllers.base import BaseOperationController
from ..core import (
    AppSettings,
    CancellationToken,
    OperationEvent,
    OperationPhase,
    OperationReporter,
    OperationResult,
    OperationState,
)
from ..runtime_paths import RuntimePaths, get_runtime_paths
from ..services.catalog_service import (
    DEFAULT_PAGE_SIZE,
    CatalogBucket,
    CatalogIndexResult,
    CatalogSearchItem,
    CatalogSearchPage,
    CatalogService,
    CatalogStorageAnalytics,
    CatalogVolumeSummary,
)
from ..services.duplicate_workflow import format_duplicate_size
from .pages import BasePage
from .theme import Spacing
from .widgets import (
    Card,
    InlineMessage,
    MetricCard,
    PathSelector,
    PrimaryButton,
    ProgressPanel,
    ResponsiveGrid,
    SectionHeader,
    ToastBanner,
    format_eta,
    set_accessible,
)


def _format_bytes_detailed(num_bytes: int) -> str:
    size = max(0, int(num_bytes))
    if size == 0:
        return "0 B"
    if size < 1024:
        return f"{size:,} B"
    return f"{format_duplicate_size(size)} ({size:,} B)"


def _empty_analytics() -> CatalogStorageAnalytics:
    return CatalogStorageAnalytics(
        total_files=0,
        total_bytes=0,
        by_media_kind=(),
        by_volume=(),
        by_capture_year=(),
        by_extension=(),
        recoverable_duplicate_bytes=None,
        duplicate_analytics_available=False,
        recoverable_duplicate_status="unavailable",
        hashed_files=0,
        unhashed_files=0,
        volumes=(),
        offline_volumes=(),
    )


def _empty_search_page(limit: int = DEFAULT_PAGE_SIZE) -> CatalogSearchPage:
    return CatalogSearchPage(
        items=(),
        total_count=0,
        limit=limit,
        offset=0,
        page=0,
        total_pages=0,
        has_next=False,
        has_previous=False,
    )


class LibraryController(BaseOperationController):
    """Qt operation controller executing catalog indexing, search, and analytics off the main thread."""

    def __init__(
        self,
        service: CatalogService,
        thread_pool: QThreadPool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(thread_pool, parent)
        self.service = service

    def start_index(
        self,
        folder_path: str | Path,
        *,
        volume_key: str = "",
        label: str = "",
        filesystem: str = "",
        compute_hashes: bool = False,
        search_params: dict[str, Any] | None = None,
    ) -> bool:
        folder_str = str(folder_path)
        query_kwargs = dict(search_params or {})

        def _task(
            cancellation: CancellationToken,
            reporter: OperationReporter,
        ) -> OperationResult:
            reporter.set_state(
                OperationState.SCANNING,
                phase=OperationPhase.DISCOVERY,
                message=f"Indexing {folder_str}…",
            )
            index_res = self.service.index_folder(
                folder_str,
                volume_key=volume_key,
                label=label,
                filesystem=filesystem,
                compute_hashes=compute_hashes,
                cancellation=cancellation,
                reporter=reporter,
            )
            if index_res.cancelled or cancellation.is_cancelled():
                return OperationResult(
                    status=OperationState.CANCELLED,
                    counts={
                        "added": index_res.added,
                        "updated": index_res.updated,
                        "unchanged": index_res.unchanged,
                        "removed": index_res.removed,
                    },
                    warnings=index_res.warnings,
                    data={
                        "operation": "index",
                        "index_result": index_res,
                    },
                )

            analytics = self.service.get_storage_analytics(stop_event=cancellation)
            search_page = self.service.search(stop_event=cancellation, **query_kwargs)
            if cancellation.is_cancelled():
                return OperationResult(
                    status=OperationState.CANCELLED,
                    data={"operation": "index", "index_result": index_res},
                )
            reporter.set_state(
                OperationState.COMPLETED,
                phase=OperationPhase.FINALIZATION,
                message="Indexing complete.",
            )
            return OperationResult(
                status=OperationState.COMPLETED,
                counts={
                    "added": index_res.added,
                    "updated": index_res.updated,
                    "unchanged": index_res.unchanged,
                    "removed": index_res.removed,
                },
                warnings=index_res.warnings,
                data={
                    "operation": "index",
                    "index_result": index_res,
                    "analytics": analytics,
                    "search_page": search_page,
                },
            )

        return self.start_task(_task)

    def start_search(self, **search_kwargs: Any) -> bool:
        kwargs = dict(search_kwargs)

        def _task(
            cancellation: CancellationToken,
            reporter: OperationReporter,
        ) -> OperationResult:
            reporter.set_state(
                OperationState.SCANNING,
                phase=OperationPhase.DISCOVERY,
                message="Searching catalog…",
            )
            page = self.service.search(stop_event=cancellation, **kwargs)
            if cancellation.is_cancelled():
                return OperationResult(
                    status=OperationState.CANCELLED,
                    data={"operation": "search"},
                )
            return OperationResult(
                status=OperationState.COMPLETED,
                counts={"total_count": page.total_count, "returned": len(page.items)},
                data={"operation": "search", "search_page": page},
            )

        return self.start_task(_task)

    def start_refresh(self, **search_kwargs: Any) -> bool:
        kwargs = dict(search_kwargs)

        def _task(
            cancellation: CancellationToken,
            reporter: OperationReporter,
        ) -> OperationResult:
            reporter.set_state(
                OperationState.SCANNING,
                phase=OperationPhase.DISCOVERY,
                message="Loading catalog summary…",
            )
            analytics = self.service.get_storage_analytics(stop_event=cancellation)
            page = self.service.search(stop_event=cancellation, **kwargs)
            if cancellation.is_cancelled():
                return OperationResult(
                    status=OperationState.CANCELLED,
                    data={"operation": "refresh"},
                )
            return OperationResult(
                status=OperationState.COMPLETED,
                data={
                    "operation": "refresh",
                    "analytics": analytics,
                    "search_page": page,
                },
            )

        return self.start_task(_task)

    def wait_for_done(self, timeout_ms: int = -1) -> bool:
        done = super().wait_for_done(timeout_ms)
        app = QApplication.instance()
        if app is not None:
            for _ in range(10):
                app.processEvents()
        return done


class LibraryPage(BasePage):
    """Self-contained Library Catalog page showing storage analytics and paged search."""

    index_completed = Signal(object)
    search_completed = Signal(object)

    def __init__(
        self,
        paths: RuntimePaths | CatalogService | str | Path | None = None,
        settings: AppSettings | None = None,
        *,
        service: CatalogService | None = None,
        controller: LibraryController | None = None,
        search_controller: LibraryController | None = None,
        db_path: str | Path | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "Library Catalog",
            "Index media folders incrementally, inspect storage analytics across online and offline volumes, and search your catalog.",
            parent,
        )
        if isinstance(paths, CatalogService):
            service = paths
            paths = None

        self.settings = settings or AppSettings()
        if service is not None:
            self.service = service
            self.paths = service.paths
        else:
            self.paths = (
                paths
                if isinstance(paths, RuntimePaths)
                else get_runtime_paths(paths)
                if paths is not None
                else get_runtime_paths()
            )
            self.service = CatalogService(self.paths, db_path=db_path)

        self.controller = controller or LibraryController(self.service, parent=self)
        self.search_controller = search_controller or LibraryController(
            self.service,
            thread_pool=self.controller._thread_pool,
            parent=self,
        )

        self._page_size = max(1, int(page_size))
        self._current_page = 1
        self.analytics_data: CatalogStorageAnalytics = _empty_analytics()
        self.search_page_data: CatalogSearchPage = _empty_search_page(self._page_size)
        self.last_index_result: CatalogIndexResult | None = None

        self._build_indexing_section()
        self._build_banners()
        self._build_summary_section()
        self._build_search_section()
        self._configure_focus_order()
        self._connect_signals()

        # Render initial empty state immediately so the summary is valid before any index exists.
        self._apply_analytics(self.analytics_data)
        self._apply_search_page(self.search_page_data)

        # If the catalog database already exists with data on disk, load its state.
        if (
            str(self.service.db_path) == ":memory:"
            or (self.service.db_path.exists() and self.service.db_path.stat().st_size > 0)
        ):
            self.refresh()
            self.wait_for_done(2000)

        self.finish()

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_indexing_section(self) -> None:
        card = Card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(Spacing.LG, Spacing.LG, Spacing.LG, Spacing.LG)
        layout.setSpacing(Spacing.MD)

        layout.addWidget(
            SectionHeader(
                "Index a media folder",
                "Read-only incremental scan: only new or modified files are read, and unmounted drives remain preserved.",
            )
        )

        self.folder_selector = PathSelector(
            "Folder to index",
            "Choose a local folder or mounted drive…",
            "Source files are never modified, moved, renamed, or deleted.",
        )
        self.path_selector = self.folder_selector
        self.path = self.folder_selector
        self.folder_selector.browse_requested.connect(self._browse_folder)
        layout.addWidget(self.folder_selector)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(Spacing.MD)

        self.compute_hashes_check = QCheckBox(
            "Compute content hashes (SHA-256) to calculate recoverable duplicate bytes"
        )
        self.compute_hashes_check.setChecked(False)
        set_accessible(
            self.compute_hashes_check,
            "Compute content hashes",
            "Optional. Off by default because whole-file hashing is expensive. Enable to calculate exact duplicate bytes.",
        )
        self.hash_checkbox = self.compute_hashes_check

        self.refresh_button = QPushButton("Refresh summary")
        set_accessible(
            self.refresh_button,
            "Refresh library summary",
            "Reload storage analytics and search results from the catalog.",
        )

        self.index_button = PrimaryButton("Index folder")
        set_accessible(
            self.index_button,
            "Index folder",
            "Start incremental catalog indexing in the background.",
        )

        controls_row.addWidget(self.compute_hashes_check, 1)
        controls_row.addWidget(self.refresh_button)
        controls_row.addWidget(self.index_button)
        layout.addLayout(controls_row)

        self.content.addWidget(card)

        self.progress_panel = ProgressPanel()
        self.progress_panel.progress.setRange(0, 100)
        self.progress_panel.progress.setValue(0)
        set_accessible(self.progress_panel, "Indexing progress panel")
        set_accessible(
            self.progress_panel.cancel_button,
            "Cancel indexing",
            "Stop the active catalog indexing operation safely.",
        )
        self.cancel_button = self.progress_panel.cancel_button
        self.progress_panel.hide()
        self.content.addWidget(self.progress_panel)

    def _build_banners(self) -> None:
        self.banner = ToastBanner("Library status messages appear here.", "info")
        set_accessible(self.banner, "Library status banner")
        self.banner.hide()
        self.content.addWidget(self.banner)

        self.offline_banner = InlineMessage(
            "All indexed volumes are currently online.",
            "warning",
        )
        set_accessible(
            self.offline_banner,
            "Offline volumes notice",
            "Indicates cataloged volumes that are currently disconnected or unmounted.",
        )
        self.offline_banner.hide()
        self.content.addWidget(self.offline_banner)

    def _build_summary_section(self) -> None:
        summary_card = Card()
        layout = QVBoxLayout(summary_card)
        layout.setContentsMargins(Spacing.LG, Spacing.LG, Spacing.LG, Spacing.LG)
        layout.setSpacing(Spacing.MD)

        layout.addWidget(
            SectionHeader(
                "Storage Summary",
                "Catalog totals by media kind, volume, capture year, extension, and duplicate status.",
            )
        )

        self.empty_summary_label = QLabel(
            "No library index present yet (0 files, 0 B). Choose a folder above to index your media library."
        )
        self.empty_summary_label.setProperty("muted", True)
        self.empty_summary_label.setWordWrap(True)
        set_accessible(self.empty_summary_label, "Empty library summary status")
        layout.addWidget(self.empty_summary_label)

        self.total_files_card = MetricCard("0 files", "Indexed files", "Across all cataloged volumes")
        set_accessible(self.total_files_card, "Total indexed files metric")

        self.total_bytes_card = MetricCard("0 B", "Total library size", "0 B")
        set_accessible(self.total_bytes_card, "Total library size metric")

        self.volumes_count_card = MetricCard("0 volumes", "Indexed volumes", "All online")
        set_accessible(self.volumes_count_card, "Indexed volumes metric")

        self.recoverable_bytes_card = MetricCard(
            "Unavailable (hashing disabled)",
            "Recoverable duplicate bytes",
            "Content hashing is off by default during indexing.",
        )
        set_accessible(
            self.recoverable_bytes_card,
            "Recoverable duplicate bytes metric",
            "Shows recoverable duplicate space when hashing was enabled, or Unavailable when hashing was off.",
        )

        self.metric_grid = ResponsiveGrid(
            [
                self.total_files_card,
                self.total_bytes_card,
                self.volumes_count_card,
                self.recoverable_bytes_card,
            ],
            min_column_width=220,
            max_columns=4,
        )
        layout.addWidget(self.metric_grid)

        self.volumes_table = self._create_summary_table(
            ["Volume", "Status", "Files", "Total Size", "Root Path"],
            accessible_name="Storage by volume table",
            min_height=130,
        )
        layout.addWidget(QLabel("By Volume (online and offline volumes)"))
        layout.addWidget(self.volumes_table)

        breakdown_grid = QGridLayout()
        breakdown_grid.setSpacing(Spacing.MD)

        self.media_kind_table = self._create_summary_table(
            ["Media Kind", "Files", "Total Size"],
            accessible_name="Storage by media kind table",
        )
        self.capture_year_table = self._create_summary_table(
            ["Capture Year", "Files", "Total Size"],
            accessible_name="Storage by capture year table",
        )
        self.extension_table = self._create_summary_table(
            ["Extension", "Files", "Total Size"],
            accessible_name="Storage by extension table",
        )

        kind_box = QWidget()
        kind_layout = QVBoxLayout(kind_box)
        kind_layout.setContentsMargins(0, 0, 0, 0)
        kind_layout.addWidget(QLabel("By Media Kind"))
        kind_layout.addWidget(self.media_kind_table)

        year_box = QWidget()
        year_layout = QVBoxLayout(year_box)
        year_layout.setContentsMargins(0, 0, 0, 0)
        year_layout.addWidget(QLabel("By Capture Year"))
        year_layout.addWidget(self.capture_year_table)

        ext_box = QWidget()
        ext_layout = QVBoxLayout(ext_box)
        ext_layout.setContentsMargins(0, 0, 0, 0)
        ext_layout.addWidget(QLabel("By Extension"))
        ext_layout.addWidget(self.extension_table)

        breakdown_grid.addWidget(kind_box, 0, 0)
        breakdown_grid.addWidget(year_box, 0, 1)
        breakdown_grid.addWidget(ext_box, 0, 2)
        layout.addLayout(breakdown_grid)

        self.content.addWidget(summary_card)

    @staticmethod
    def _create_summary_table(
        headers: list[str],
        *,
        accessible_name: str,
        min_height: int = 120,
    ) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setMinimumHeight(min_height)
        set_accessible(table, accessible_name)
        return table

    def _build_search_section(self) -> None:
        search_card = Card()
        layout = QVBoxLayout(search_card)
        layout.setContentsMargins(Spacing.LG, Spacing.LG, Spacing.LG, Spacing.LG)
        layout.setSpacing(Spacing.MD)

        layout.addWidget(
            SectionHeader(
                "Search Catalog",
                "Filter indexed files by media kind, filename, size range, and capture date range with paged results.",
            )
        )

        filter_grid = QGridLayout()
        filter_grid.setSpacing(Spacing.SM)

        self.media_kind_filter = QComboBox()
        self.media_kind_filter.addItem("All media kinds", "")
        self.media_kind_filter.addItem("Images", "image")
        self.media_kind_filter.addItem("Videos", "video")
        self.media_kind_filter.addItem("Audio", "audio")
        self.media_kind_filter.addItem("Documents", "document")
        self.media_kind_filter.addItem("Other", "other")
        set_accessible(
            self.media_kind_filter,
            "Filter by media kind",
            "Choose a media kind to filter catalog search results.",
        )

        self.filename_filter = QLineEdit()
        self.filename_filter.setPlaceholderText("Filename contains…")
        set_accessible(
            self.filename_filter,
            "Filter by filename",
            "Case-insensitive substring match on filename.",
        )

        self.min_size_filter = QSpinBox()
        self.min_size_filter.setRange(0, 2_000_000_000)
        self.min_size_filter.setSingleStep(1024)
        self.min_size_filter.setSuffix(" B")
        self.min_size_filter.setSpecialValueText("No min")
        self.min_size_filter.setValue(0)
        set_accessible(
            self.min_size_filter,
            "Minimum file size in bytes",
            "Minimum file size filter in bytes (0 means no minimum).",
        )

        self.max_size_filter = QSpinBox()
        self.max_size_filter.setRange(0, 2_000_000_000)
        self.max_size_filter.setSingleStep(1024)
        self.max_size_filter.setSuffix(" B")
        self.max_size_filter.setSpecialValueText("No max")
        self.max_size_filter.setValue(0)
        set_accessible(
            self.max_size_filter,
            "Maximum file size in bytes",
            "Maximum file size filter in bytes (0 means no maximum).",
        )

        self.date_start_filter = QLineEdit()
        self.date_start_filter.setPlaceholderText("YYYY-MM-DD (from)")
        set_accessible(
            self.date_start_filter,
            "Capture start date",
            "Filter files captured on or after this date (YYYY-MM-DD).",
        )

        self.date_end_filter = QLineEdit()
        self.date_end_filter.setPlaceholderText("YYYY-MM-DD (to)")
        set_accessible(
            self.date_end_filter,
            "Capture end date",
            "Filter files captured on or before this date (YYYY-MM-DD).",
        )

        filter_grid.addWidget(QLabel("Media kind"), 0, 0)
        filter_grid.addWidget(self.media_kind_filter, 0, 1)
        filter_grid.addWidget(QLabel("Filename"), 0, 2)
        filter_grid.addWidget(self.filename_filter, 0, 3)

        filter_grid.addWidget(QLabel("Min size"), 1, 0)
        filter_grid.addWidget(self.min_size_filter, 1, 1)
        filter_grid.addWidget(QLabel("Max size"), 1, 2)
        filter_grid.addWidget(self.max_size_filter, 1, 3)

        filter_grid.addWidget(QLabel("Date from"), 2, 0)
        filter_grid.addWidget(self.date_start_filter, 2, 1)
        filter_grid.addWidget(QLabel("Date to"), 2, 2)
        filter_grid.addWidget(self.date_end_filter, 2, 3)

        layout.addLayout(filter_grid)

        action_row = QHBoxLayout()
        action_row.setSpacing(Spacing.SM)

        self.page_size_combo = QComboBox()
        for size in sorted({10, 25, 50, 100, self._page_size}):
            self.page_size_combo.addItem(f"{size} per page", size)
        idx = self.page_size_combo.findData(self._page_size)
        if idx >= 0:
            self.page_size_combo.setCurrentIndex(idx)
        set_accessible(
            self.page_size_combo,
            "Results per page",
            "Number of catalog search results displayed per page.",
        )

        self.clear_filters_button = QPushButton("Clear filters")
        set_accessible(
            self.clear_filters_button,
            "Clear search filters",
            "Reset all search filter fields and reload the first page.",
        )

        self.search_button = PrimaryButton("Search")
        set_accessible(
            self.search_button,
            "Search catalog",
            "Run a paged search with the selected filters.",
        )

        action_row.addWidget(QLabel("Page size"))
        action_row.addWidget(self.page_size_combo)
        action_row.addStretch(1)
        action_row.addWidget(self.clear_filters_button)
        action_row.addWidget(self.search_button)
        layout.addLayout(action_row)

        headers = [
            "Filename",
            "Media Kind",
            "Size",
            "Capture Date",
            "Volume",
            "Volume Status",
            "Relative Path",
        ]
        self.results_table = QTableWidget(0, len(headers))
        self.results_table.setHorizontalHeaderLabels(headers)
        self.results_table.verticalHeader().setVisible(False)
        self.results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.results_table.setMinimumHeight(240)
        set_accessible(
            self.results_table,
            "Catalog search results table",
            "Paged table of matching files in the library catalog.",
        )
        self.table = self.results_table
        layout.addWidget(self.results_table)

        pager_row = QHBoxLayout()
        pager_row.setSpacing(Spacing.SM)

        self.prev_page_button = QPushButton("Previous page")
        self.prev_page_button.setEnabled(False)
        set_accessible(
            self.prev_page_button,
            "Previous results page",
            "Load the previous page of catalog search results.",
        )

        self.page_label = QLabel("Page 0 of 0 • 0 matching files")
        self.page_label.setProperty("muted", True)
        set_accessible(self.page_label, "Pagination status")

        self.next_page_button = QPushButton("Next page")
        self.next_page_button.setEnabled(False)
        set_accessible(
            self.next_page_button,
            "Next results page",
            "Load the next page of catalog search results.",
        )

        pager_row.addWidget(self.prev_page_button)
        pager_row.addStretch(1)
        pager_row.addWidget(self.page_label)
        pager_row.addStretch(1)
        pager_row.addWidget(self.next_page_button)
        layout.addLayout(pager_row)

        self.content.addWidget(search_card)

    def _configure_focus_order(self) -> None:
        focus_chain = [
            self.folder_selector.entry,
            self.folder_selector.browse_button,
            self.compute_hashes_check,
            self.refresh_button,
            self.index_button,
            self.media_kind_filter,
            self.filename_filter,
            self.min_size_filter,
            self.max_size_filter,
            self.date_start_filter,
            self.date_end_filter,
            self.page_size_combo,
            self.clear_filters_button,
            self.search_button,
            self.results_table,
            self.prev_page_button,
            self.next_page_button,
        ]
        for first, second in zip(focus_chain, focus_chain[1:]):
            QWidget.setTabOrder(first, second)

    def _connect_signals(self) -> None:
        self.index_button.clicked.connect(lambda: self.start_index())
        self.refresh_button.clicked.connect(self.refresh)
        self.progress_panel.cancel_requested.connect(self.cancel_indexing)

        self.search_button.clicked.connect(lambda: self.start_search(page=1))
        self.clear_filters_button.clicked.connect(self.clear_filters)
        self.filename_filter.returnPressed.connect(lambda: self.start_search(page=1))
        self.page_size_combo.currentIndexChanged.connect(self._on_page_size_changed)
        self.prev_page_button.clicked.connect(self.previous_page)
        self.next_page_button.clicked.connect(self.next_page)

        for ctrl in (self.controller, self.search_controller):
            ctrl.progress.connect(self._on_progress)
            ctrl.completed.connect(self._on_completed)
            ctrl.cancelled.connect(self._on_cancelled)
            ctrl.failed.connect(self._on_failed)
            ctrl.recoverable_error.connect(self._on_recoverable_error)

        self.controller.busy_changed.connect(self._on_index_busy_changed)

    # ------------------------------------------------------------------
    # Actions & Worker Dispatch
    # ------------------------------------------------------------------

    def _browse_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self,
            "Choose folder to index",
            self.folder_selector.path() or str(Path.home()),
        )
        if chosen:
            self.folder_selector.set_path(chosen)

    def _collect_search_kwargs(self, *, page: int | None = None) -> dict[str, Any]:
        target_page = max(1, int(page if page is not None else self._current_page))
        media_kind = str(self.media_kind_filter.currentData() or "").strip() or None
        filename = self.filename_filter.text().strip() or None
        min_size_val = int(self.min_size_filter.value())
        max_size_val = int(self.max_size_filter.value())
        min_size = min_size_val if min_size_val > 0 else None
        max_size = max_size_val if max_size_val > 0 else None
        cap_start = self.date_start_filter.text().strip() or None
        cap_end = self.date_end_filter.text().strip() or None

        return {
            "media_kind": media_kind,
            "filename": filename,
            "min_size": min_size,
            "max_size": max_size,
            "capture_start": cap_start,
            "capture_end": cap_end,
            "page": target_page,
            "page_size": self._page_size,
        }

    def start_index(
        self,
        folder_path: str | Path | None = None,
        *,
        volume_key: str = "",
        label: str = "",
        filesystem: str = "",
        compute_hashes: bool | None = None,
    ) -> bool:
        """Start indexing ``folder_path`` on a Qt worker thread."""
        if folder_path is not None:
            self.folder_selector.set_path(str(folder_path))
        if compute_hashes is not None:
            self.compute_hashes_check.setChecked(bool(compute_hashes))

        target = self.folder_selector.path()
        if not target:
            self.folder_selector.set_error("Choose a folder to index.")
            self._show_message("Choose a folder to index first.", "warning")
            return False

        self.folder_selector.set_error("")
        eff_hashes = self.compute_hashes_check.isChecked()
        self._current_page = 1
        search_params = self._collect_search_kwargs(page=1)

        # If a background search/refresh worker is running, cancel and wait briefly
        if self.search_controller.busy:
            self.search_controller.cancel()
            self.search_controller.wait_for_done(1000)

        self.progress_panel.progress.setRange(0, 100)
        self.progress_panel.update_progress(
            0,
            "Indexing folder…",
            target,
            "0%  •  0 items  •  ETA —",
        )
        self.progress_panel.show()
        self.banner.hide()

        started = self.controller.start_index(
            target,
            volume_key=volume_key,
            label=label,
            filesystem=filesystem,
            compute_hashes=eff_hashes,
            search_params=search_params,
        )
        if not started:
            self.progress_panel.hide()
        return started

    index_folder = start_index

    def cancel_indexing(self) -> bool:
        """Request cooperative cancellation of the active indexing operation."""
        cancelled = self.controller.cancel()
        if self.search_controller.busy:
            cancelled = self.search_controller.cancel() or cancelled
        return cancelled

    cancel = cancel_indexing

    def start_search(self, *, page: int | None = None) -> bool:
        """Run a paged catalog search on a Qt worker thread."""
        if page is not None:
            self._current_page = max(1, int(page))
        try:
            kwargs = self._collect_search_kwargs(page=self._current_page)
        except ValueError as exc:
            self._show_message(f"Invalid search filter: {exc}", "warning")
            return False

        if self.search_controller.busy:
            self.search_controller.cancel()
            self.search_controller.wait_for_done(1000)

        return self.search_controller.start_search(**kwargs)

    search = start_search

    def refresh(self) -> bool:
        """Reload storage analytics and the current search page on a Qt worker thread."""
        if self.controller.busy:
            return False
        if self.search_controller.busy:
            self.search_controller.cancel()
            self.search_controller.wait_for_done(1000)
        kwargs = self._collect_search_kwargs(page=self._current_page)
        return self.search_controller.start_refresh(**kwargs)

    def clear_filters(self) -> None:
        self.media_kind_filter.setCurrentIndex(0)
        self.filename_filter.clear()
        self.min_size_filter.setValue(0)
        self.max_size_filter.setValue(0)
        self.date_start_filter.clear()
        self.date_end_filter.clear()
        self._current_page = 1
        self.start_search(page=1)

    def set_page_size(self, page_size: int) -> None:
        """Update the number of search rows per page and reload page 1."""
        size = max(1, int(page_size))
        self._page_size = size
        self.page_size_combo.blockSignals(True)
        idx = self.page_size_combo.findData(size)
        if idx < 0:
            self.page_size_combo.addItem(f"{size} per page", size)
            idx = self.page_size_combo.findData(size)
        self.page_size_combo.setCurrentIndex(idx)
        self.page_size_combo.blockSignals(False)
        self._current_page = 1
        self.start_search(page=1)

    def _on_page_size_changed(self) -> None:
        data = self.page_size_combo.currentData()
        if data is not None:
            self._page_size = max(1, int(data))
            self._current_page = 1
            self.start_search(page=1)

    def next_page(self) -> bool:
        if not self.search_page_data.has_next:
            return False
        return self.start_search(page=self._current_page + 1)

    def previous_page(self) -> bool:
        if not self.search_page_data.has_previous or self._current_page <= 1:
            return False
        return self.start_search(page=self._current_page - 1)

    def go_to_page(self, page_number: int) -> bool:
        return self.start_search(page=max(1, int(page_number)))

    def wait_for_done(self, timeout_ms: int = 5000) -> bool:
        ok1 = self.controller.wait_for_done(timeout_ms)
        ok2 = self.search_controller.wait_for_done(timeout_ms)
        app = QApplication.instance()
        if app is not None:
            for _ in range(10):
                app.processEvents()
        return ok1 and ok2

    # ------------------------------------------------------------------
    # Slots & Rendering (Main Thread Only)
    # ------------------------------------------------------------------

    def _on_index_busy_changed(self, busy: bool) -> None:
        self.index_button.setEnabled(not busy)
        self.refresh_button.setEnabled(not busy)
        self.folder_selector.entry.setEnabled(not busy)
        self.folder_selector.browse_button.setEnabled(not busy)
        self.compute_hashes_check.setEnabled(not busy)

    def _on_progress(self, event: OperationEvent) -> None:
        if not self.controller.busy:
            return
        self.progress_panel.progress.setRange(0, 100)
        if event.progress is not None:
            pct = max(0, min(100, int(round(event.progress * 100))))
        elif event.total_items > 0:
            pct = max(0, min(100, int(round((event.processed_items / event.total_items) * 100))))
        else:
            pct = 0
        metrics = (
            f"{pct}%  •  {event.processed_items:,}/{max(event.total_items, event.processed_items):,} items"
            f"  •  ETA {format_eta(event.eta_seconds)}"
        )
        self.progress_panel.update_progress(
            pct,
            event.message or "Indexing…",
            event.current_item or self.folder_selector.path(),
            metrics,
        )

    def _on_completed(self, result: OperationResult) -> None:
        data = result.data or {}
        op = data.get("operation", "")
        if op == "index":
            self.progress_panel.hide()
            index_res: CatalogIndexResult | None = data.get("index_result")
            self.last_index_result = index_res
            analytics: CatalogStorageAnalytics | None = data.get("analytics")
            search_page: CatalogSearchPage | None = data.get("search_page")
            if analytics is not None:
                self._apply_analytics(analytics)
            if search_page is not None:
                self._apply_search_page(search_page)
            if index_res is not None:
                if index_res.offline:
                    self._show_message(
                        f"Volume '{index_res.volume_key}' is currently offline (not mounted). "
                        "Existing catalog entries were preserved untouched.",
                        "warning",
                    )
                else:
                    self._show_message(
                        f"Indexed {index_res.root_path}: {index_res.added} added, "
                        f"{index_res.updated} updated, {index_res.unchanged} unchanged.",
                        "success",
                    )
            self.index_completed.emit(index_res)
        elif op == "refresh":
            analytics = data.get("analytics")
            search_page = data.get("search_page")
            if analytics is not None:
                self._apply_analytics(analytics)
            if search_page is not None:
                self._apply_search_page(search_page)
        elif op == "search":
            search_page = data.get("search_page")
            if search_page is not None:
                self._apply_search_page(search_page)
                self.search_completed.emit(search_page)

    def _on_cancelled(self, result: OperationResult) -> None:
        data = result.data or {}
        op = data.get("operation", "index")
        if op == "index" or not self.controller.busy:
            self.progress_panel.hide()
            self._show_message(
                "Indexing cancelled. The catalog database was left in a consistent state.",
                "warning",
            )

    def _on_failed(self, error: Any) -> None:
        self.progress_panel.hide()
        message = getattr(error, "message", str(error))
        self._show_message(message, "danger")

    def _on_recoverable_error(self, error: Any) -> None:
        message = getattr(error, "message", str(error))
        self._show_message(message, "warning")

    def _apply_analytics(self, analytics: CatalogStorageAnalytics) -> None:
        self.analytics_data = analytics
        has_any_data = analytics.total_files > 0 or len(analytics.volumes) > 0
        self.empty_summary_label.setVisible(not has_any_data)

        self.total_files_card.value_label.setText(f"{analytics.total_files:,} files")
        self.total_bytes_card.value_label.setText(format_duplicate_size(analytics.total_bytes))

        vol_count = len(analytics.volumes)
        offline_count = len(analytics.offline_volumes)
        if offline_count > 0:
            self.volumes_count_card.value_label.setText(
                f"{vol_count} volume(s) ({offline_count} offline)"
            )
            offline_names = ", ".join(
                f"{vol.label or vol.volume_key} ({vol.file_count:,} files)"
                for vol in analytics.offline_volumes
            )
            self.offline_banner.set_message(
                f"Offline volume(s) detected: {offline_names}. "
                "Unmounted drives are marked Offline and their catalog rows are preserved.",
                "warning",
            )
            self.offline_banner.show()
        else:
            self.volumes_count_card.value_label.setText(f"{vol_count} volume(s)")
            self.offline_banner.hide()

        if analytics.duplicate_analytics_available and analytics.recoverable_duplicate_bytes is not None:
            self.recoverable_bytes_card.value_label.setText(
                _format_bytes_detailed(analytics.recoverable_duplicate_bytes)
            )
        else:
            self.recoverable_bytes_card.value_label.setText("Unavailable (hashing disabled)")

        self._populate_volumes_table(analytics.volumes)
        self._populate_bucket_table(self.media_kind_table, analytics.by_media_kind)
        self._populate_bucket_table(self.capture_year_table, analytics.by_capture_year)
        self._populate_bucket_table(self.extension_table, analytics.by_extension)

    def _populate_volumes_table(self, volumes: tuple[CatalogVolumeSummary, ...]) -> None:
        self.volumes_table.setRowCount(len(volumes))
        for row_idx, vol in enumerate(volumes):
            status_text = "Online" if vol.is_online else "Offline (not mounted)"
            name_text = (
                f"{vol.label or vol.volume_key}"
                if vol.is_online
                else f"{vol.label or vol.volume_key} [Offline]"
            )
            cells = [
                name_text,
                status_text,
                f"{vol.file_count:,}",
                _format_bytes_detailed(vol.total_bytes),
                vol.root_path or "—",
            ]
            for col_idx, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, vol.volume_key)
                self.volumes_table.setItem(row_idx, col_idx, item)

    @staticmethod
    def _populate_bucket_table(
        table: QTableWidget,
        buckets: tuple[CatalogBucket, ...],
    ) -> None:
        table.setRowCount(len(buckets))
        for row_idx, bucket in enumerate(buckets):
            label = bucket.label or (str(bucket.key) if bucket.key is not None else "Unknown")
            cells = [
                label,
                f"{bucket.file_count:,}",
                _format_bytes_detailed(bucket.total_bytes),
            ]
            for col_idx, text in enumerate(cells):
                table.setItem(row_idx, col_idx, QTableWidgetItem(text))

    def _apply_search_page(self, page: CatalogSearchPage) -> None:
        self.search_page_data = page
        if page.page > 0:
            self._current_page = page.page

        items: tuple[CatalogSearchItem, ...] = page.items
        self.results_table.setRowCount(len(items))
        for row_idx, rec in enumerate(items):
            status_text = "Online" if rec.volume_online else "Offline (not mounted)"
            vol_text = (
                rec.volume_label or rec.volume_key
                if rec.volume_online
                else f"{rec.volume_label or rec.volume_key} [Offline]"
            )
            cap_text = rec.capture_date[:10] if rec.capture_date else "—"
            cells = [
                rec.filename,
                rec.media_kind.title(),
                _format_bytes_detailed(rec.size),
                cap_text,
                vol_text,
                status_text,
                rec.relative_path,
            ]
            for col_idx, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                cell.setData(Qt.ItemDataRole.UserRole, rec.id)
                self.results_table.setItem(row_idx, col_idx, cell)

        if page.total_count == 0:
            self.page_label.setText("Page 0 of 0 • 0 matching files")
        else:
            start_idx = page.offset + 1
            end_idx = page.offset + len(items)
            self.page_label.setText(
                f"Page {page.page} of {page.total_pages} • Showing {start_idx}–{end_idx} of {page.total_count:,} matching files"
            )

        self.prev_page_button.setEnabled(page.has_previous)
        self.next_page_button.setEnabled(page.has_next)

    def _show_message(self, message: str, kind: str = "info") -> None:
        self.banner.set_message(message, kind)
        self.banner.show()


__all__ = ["LibraryController", "LibraryPage"]
