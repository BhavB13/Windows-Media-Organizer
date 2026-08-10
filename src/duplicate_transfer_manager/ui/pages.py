"""Connected static screens for the Phase 2 application shell."""

from __future__ import annotations

from collections import deque
from pathlib import Path

from PySide6.QtCore import QThreadPool, QTimer, Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QDialog,
    QDialogButtonBox,
    QInputDialog,
)

from models import Settings
from transfer_safety import cleanup_partial_files
from utils import DEFAULT_EXCLUDES, DEFAULT_MEDIA_EXTS, HashCache

from ..controllers import DuplicateScanController, TransferController
from ..controllers.base import OperationWorker
from ..core import AppSettings, CancellationToken, OperationResult, OperationState
from ..runtime_paths import get_runtime_paths
from ..sorting import SortExecutor
from ..services import (
    DeviceService,
    DiagnosticsService,
    DuplicateQuarantineService,
    DuplicateReview,
    DashboardService,
    FileOrganizerService,
    IOSTransferService,
    OperationRecordService,
    ReportService,
    ScheduledScanService,
    SettingsService,
    STAGE_LABELS,
    TRANSFER_PROFILES,
    build_import_review,
    build_import_settings,
    build_duplicate_review,
    format_duplicate_size,
    summarize_transfer_result,
)
from ..version import __version__
from .icons import icon, icon_size
from .theme import Spacing, ThemeManager
from .widgets import (
    Card,
    CompletionSummary,
    DisclosurePanel,
    EmptyState,
    InlineMessage,
    MetricCard,
    PageHeader,
    PathSelector,
    PrimaryButton,
    ProgressPanel,
    ResponsiveGrid,
    SectionHeader,
    SourcePicker,
    StepIndicator,
    ToastBanner,
    format_eta,
)


class BasePage(QScrollArea):
    def __init__(self, title: str, subtitle: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport_container = QWidget()
        self.viewport_container.setObjectName("PageCanvas")
        self.viewport_container.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        viewport_layout = QHBoxLayout(self.viewport_container)
        viewport_layout.setContentsMargins(0, 0, 0, 0)
        self.canvas = QWidget()
        self.canvas.setObjectName("PageCanvas")
        self.canvas.setMaximumWidth(1480)
        self.canvas.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        self.content = QVBoxLayout(self.canvas)
        self.content.setContentsMargins(
            Spacing.XL,
            Spacing.XL,
            Spacing.XL,
            Spacing.XXL,
        )
        self.content.setSpacing(Spacing.LG)
        self.content.addWidget(PageHeader(title, subtitle))
        viewport_layout.addStretch(1)
        viewport_layout.addWidget(self.canvas, 20)
        viewport_layout.addStretch(1)
        self.setWidget(self.viewport_container)

    def run_in_background(self, work, on_result, on_error=None) -> None:
        """Run a blocking service call off the Qt main thread.

        Quarantine and restore move, copy, and pull files one at a time. Called
        inline they froze the window for the whole operation, with no progress
        and no way to tell the app from a hang. ``work`` takes no arguments and
        its return value is delivered to ``on_result`` on the main thread.
        """

        worker = OperationWorker(
            lambda _cancellation, _reporter: OperationResult(
                status=OperationState.COMPLETED, data={"value": work()}
            ),
            CancellationToken(),
        )
        # QThreadPool owns the C++ runnable, but nothing owns the Python
        # wrapper or the QObject carrying its signals. Without this reference
        # they can be collected before the result is delivered and the callback
        # never runs.
        if not hasattr(self, "_background_workers"):
            self._background_workers = []
        self._background_workers.append(worker)
        worker.signals.result.connect(lambda result: on_result((result.data or {}).get("value")))
        if on_error is not None:
            worker.signals.error.connect(on_error)
        worker.signals.finished.connect(
            lambda: self._background_workers.remove(worker) if worker in self._background_workers else None
        )
        QThreadPool.globalInstance().start(worker)

    def finish(self) -> None:
        self.content.addStretch(1)


class DashboardListCard(Card):
    """Compact overview card for recent work, attention items, and storage."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMaximumHeight(360)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(Spacing.LG, Spacing.LG, Spacing.LG, Spacing.LG)
        layout.setSpacing(Spacing.MD)
        self.heading = QLabel(title)
        self.heading.setProperty("role", "section")
        self.heading.setProperty("status", "success")
        self.rows = QVBoxLayout()
        self.rows.setSpacing(Spacing.SM)
        layout.addWidget(self.heading)
        layout.addLayout(self.rows)
        layout.addStretch(1)

    def set_items(self, values: list[tuple[str, str]]) -> None:
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for label, value in values:
            row = QFrame()
            row.setProperty("subtleCard", True)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
            row_layout.setSpacing(Spacing.SM)
            title = QLabel(label)
            title.setProperty("role", "section")
            title.setWordWrap(True)
            detail = QLabel(value)
            detail.setProperty("muted", True)
            detail.setWordWrap(True)
            detail.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row_layout.addWidget(title, 2)
            row_layout.addWidget(detail, 1)
            self.rows.addWidget(row)


class OverviewPage(BasePage):
    navigate_requested = Signal(str)

    def __init__(
        self,
        dashboard_service: DashboardService | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "Overview",
            "A calm starting point for duplicate review and verified file imports.",
            parent,
        )
        self.dashboard_service = dashboard_service or DashboardService()
        self.canvas.setMaximumWidth(1240)
        self.content.setContentsMargins(
            Spacing.XL,
            Spacing.LG,
            Spacing.XL,
            Spacing.XL,
        )
        self.content.setSpacing(Spacing.MD)
        self.content.setAlignment(Qt.AlignmentFlag.AlignTop)

        hero = Card()
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(
            Spacing.LG,
            Spacing.LG,
            Spacing.LG,
            Spacing.LG,
        )
        hero_layout.setSpacing(Spacing.SM)
        hero_text = QWidget()
        text = QVBoxLayout(hero_text)
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(Spacing.SM)
        eyebrow = QLabel("SAFE, LOCAL FILE MANAGEMENT")
        eyebrow.setProperty("muted", True)
        heading = QLabel("Review, import, and recover files with confidence.")
        heading.setProperty("role", "title")
        heading.setWordWrap(True)
        detail = QLabel(
            "Find byte-for-byte duplicates, import only new files, and restore quarantined copies without leaving your PC."
        )
        detail.setProperty("role", "subtitle")
        detail.setWordWrap(True)
        actions = QHBoxLayout()
        find_button = PrimaryButton("Find duplicate files")
        find_button.setIcon(icon("duplicates", "#FFFFFF"))
        find_button.setIconSize(icon_size())
        import_button = QPushButton("Import new files")
        import_button.setIcon(icon("import"))
        import_button.setIconSize(icon_size())
        find_button.clicked.connect(lambda: self.navigate_requested.emit("duplicates"))
        import_button.clicked.connect(lambda: self.navigate_requested.emit("import"))
        organize_button = QPushButton("Sort files")
        organize_button.setIcon(icon("folder"))
        organize_button.setIconSize(icon_size())
        organize_button.clicked.connect(lambda: self.navigate_requested.emit("sort"))
        actions.addWidget(find_button)
        actions.addWidget(import_button)
        actions.addWidget(organize_button)
        actions.addStretch()
        text.addWidget(eyebrow)
        text.addWidget(heading)
        text.addWidget(detail)
        text.addSpacing(Spacing.MD)
        text.addLayout(actions)

        safety = Card(subtle=True)
        safety_layout = QVBoxLayout(safety)
        safety_layout.setContentsMargins(
            Spacing.LG,
            Spacing.LG,
            Spacing.LG,
            Spacing.LG,
        )
        safety_layout.setSpacing(Spacing.SM)
        safety_icon = QLabel()
        safety_icon.setPixmap(icon("quarantine", "#17803D", 24).pixmap(24, 24))
        safety_title = QLabel("Ready for careful cleanup")
        safety_title.setProperty("role", "section")
        safety_title.setWordWrap(True)
        safety_text = QLabel(
            "Copy-only imports • Review before scan • Recoverable quarantine • Local reports"
        )
        safety_text.setProperty("muted", True)
        safety_text.setWordWrap(True)
        safety_layout.addWidget(safety_icon)
        safety_layout.addWidget(safety_title)
        safety_layout.addWidget(safety_text)
        hero_layout.addWidget(
            ResponsiveGrid(
                [hero_text, safety],
                min_column_width=380,
                max_columns=2,
            )
        )
        self.content.addWidget(hero)

        overview_header = SectionHeader("At a glance", action_text="Refresh")
        overview_header.action_requested.connect(self.refresh)
        self.content.addWidget(overview_header)
        self.metrics = ResponsiveGrid(
            [
                MetricCard("0 B", "Space ready to recover", "Quarantined duplicates that can be restored."),
                MetricCard("0", "Recent operations", "Local scans, imports, and reports."),
                MetricCard("0", "Interrupted transfers", "Operations that may need attention or resume."),
                MetricCard("0", "Connected devices", "Android devices available for duplicate scans or imports."),
            ],
            min_column_width=240,
            max_columns=4,
        )
        self.content.addWidget(self.metrics)

        self.recent = DashboardListCard("Recent activity")
        self.interrupted = DashboardListCard("Needs attention")
        self.storage = DashboardListCard("Local storage")
        self.summary_sections = ResponsiveGrid(
            [self.recent, self.interrupted, self.storage],
            min_column_width=300,
            max_columns=3,
        )
        self.content.addWidget(self.summary_sections)
        # Overview is the landing route, so the window must not wait on a walk
        # of the cache tree that grows with every scan. Render immediately with
        # the counts already in memory, then measure storage once the shell is
        # on screen.
        self.refresh(include_storage=False)
        QTimer.singleShot(0, self.refresh)

    def refresh(self, *, include_storage: bool = True) -> None:
        summary = self.dashboard_service.summary(include_storage=include_storage)
        recent = summary["recent_operations"]
        interrupted = summary["interrupted_transfers"]
        devices = summary.get("connected_devices", [])
        self.metrics.widgets[0].layout().itemAt(0).widget().setText(format_duplicate_size(summary["recoverable_bytes"]))
        self.metrics.widgets[1].layout().itemAt(0).widget().setText(str(len(recent)))
        self.metrics.widgets[2].layout().itemAt(0).widget().setText(str(len(interrupted)))
        self.metrics.widgets[3].layout().itemAt(0).widget().setText(str(len(devices)))
        self.recent.set_items(
            [
                (
                    record.get("title", "Operation"),
                    f"{record.get('status', 'unknown')} • {record.get('created_at', '')[:19]}",
                )
                for record in recent[:6]
            ]
            or [("No activity yet", "Run a scan or import to populate the dashboard.")]
        )
        self.interrupted.set_items(
            [
                (
                    record.get("title", "Operation"),
                    "Resume available" if record.get("resume_available") else record.get("status", "needs attention"),
                )
                for record in interrupted[:6]
            ]
            or [("No interrupted transfers", "Nothing currently needs recovery.")]
        )
        storage = summary["storage"]
        measuring = not storage.get("measured", True)
        self.storage.set_items(
            [
                ("Cache", "Measuring…" if measuring else format_duplicate_size(storage["cache_bytes"])),
                ("Reports", "Measuring…" if measuring else format_duplicate_size(storage["reports_bytes"])),
                ("Quarantine", format_duplicate_size(storage["quarantine_bytes"])),
                ("Connected devices", ", ".join(str(device.get("serial", device)) for device in devices[:3]) or "None"),
            ]
        )


class DuplicatesPage(BasePage):
    def __init__(
        self,
        hash_cache: HashCache | None = None,
        quarantine_service: DuplicateQuarantineService | None = None,
        operation_service: OperationRecordService | None = None,
        settings: AppSettings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "Find Duplicates",
            "Scan first, review every group, then move selected copies into recoverable quarantine.",
            parent,
        )
        self.app_settings = settings or AppSettings()
        self.quarantine_service = quarantine_service or DuplicateQuarantineService()
        self.operation_service = operation_service or OperationRecordService(self.quarantine_service.paths)
        self.paths = self.quarantine_service.paths
        self.hash_cache = hash_cache or HashCache(str(self.paths.hash_cache))
        if hash_cache is None:
            self.hash_cache.load()
        self.controller = DuplicateScanController(self.hash_cache)
        self.review: DuplicateReview | None = None
        self.item_groups: dict[str, str] = {}
        self.item_rows: dict[str, int] = {}
        self.keep_buttons: dict[str, QRadioButton] = {}
        self.quarantine_checks: dict[str, QCheckBox] = {}
        self.keep_groups: list[QButtonGroup] = []
        self._render_queue = deque()
        self.adb_serial = ""

        self.content.addWidget(
            StepIndicator(["Source", "Options", "Review", "Scan", "Results", "Quarantine"])
        )
        self.banner = ToastBanner(
            "Review the scan setup, then run the scan. Files are never moved during scanning.",
            "info",
        )
        self.banner.hide()
        self.content.addWidget(self.banner)

        source_card = Card()
        source_layout = QVBoxLayout(source_card)
        source_layout.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.XL)
        source_layout.setSpacing(Spacing.LG)
        source_layout.addWidget(
            SectionHeader(
                "1. Select a source",
                "Scan a local folder, a drive, or an authorized Android path.",
            )
        )
        self.source_picker = SourcePicker(
            [
                ("local", "Folder or drive", "On this PC", "folder"),
                ("android", "Android device", "USB debugging", "phone"),
            ]
        )
        self.source_picker.select("local")
        self.path = PathSelector(
            "Scan location",
            "Choose a folder or drive",
            "Subfolders are included automatically.",
        )
        self.path.browse_requested.connect(self._browse)
        self.path.path_changed.connect(lambda _value: self._invalidate_review())
        self.source_picker.selection_changed.connect(self._source_changed)
        self.device_choice = QComboBox()
        self.device_choice.setAccessibleName("Android device")
        self.device_choice.hide()
        source_layout.addWidget(self.source_picker)
        source_layout.addWidget(self.device_choice)
        source_layout.addWidget(self.path)
        self.favorite_location = QComboBox()
        self.favorite_location.setAccessibleName("Saved scan locations")
        self.favorite_location.addItem("Use a saved location…", "")
        for location in self.app_settings.favorite_locations:
            self.favorite_location.addItem(location, location)
        self.favorite_location.currentIndexChanged.connect(self._choose_favorite_location)
        self.favorite_location.setVisible(bool(self.app_settings.favorite_locations))
        source_layout.addWidget(self.favorite_location)
        self.content.addWidget(source_card)

        options = Card()
        options_layout = QVBoxLayout(options)
        options_layout.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.XL)
        options_layout.setSpacing(Spacing.MD)
        options_layout.addWidget(
            SectionHeader(
                "2. Select file categories",
                "Pictures include HEIC and common camera RAW formats; unavailable previews fall back to file details.",
            )
        )
        filters = QHBoxLayout()
        self.pictures = QCheckBox("Pictures")
        self.pictures.setChecked(True)
        self.videos = QCheckBox("Videos")
        self.videos.setChecked(True)
        self.audio = QCheckBox("Audio")
        self.other = QCheckBox("Other file types")
        for widget in (self.pictures, self.videos, self.audio, self.other):
            widget.toggled.connect(lambda _checked: self._invalidate_review())
            filters.addWidget(widget)
        filters.addStretch()
        options_layout.addLayout(filters)

        keep_row = QHBoxLayout()
        keep_row.addWidget(QLabel("Default copy to keep:"))
        self.oldest = QRadioButton("Oldest")
        self.newest = QRadioButton("Newest")
        self.quality = QRadioButton("Highest resolution")
        self.oldest.setChecked(True)
        self.oldest.toggled.connect(lambda checked: checked and self._apply_preference("oldest"))
        self.newest.toggled.connect(lambda checked: checked and self._apply_preference("newest"))
        self.quality.toggled.connect(lambda checked: checked and self._apply_preference("quality"))
        keep_row.addWidget(self.oldest)
        keep_row.addWidget(self.newest)
        keep_row.addWidget(self.quality)
        keep_row.addStretch()
        options_layout.addLayout(keep_row)

        self.dry_run_quarantine = QCheckBox("Dry run quarantine — validate without moving files")
        self.dry_run_quarantine.setToolTip(
            "After the scan, confirm quarantine in preview mode: validate selected duplicates and write a manifest without moving local files or pulling Android copies."
        )
        self.dry_run_quarantine.toggled.connect(lambda _checked: self._refresh_review_summary())
        options_layout.addWidget(self.dry_run_quarantine)

        advanced = DisclosurePanel()
        advanced.body_layout.addWidget(QLabel("Hash algorithm"))
        self.hash_choice = QComboBox()
        self.hash_choice.addItem("SHA-256 — recommended", "sha256")
        self.hash_choice.addItem("MD5 — compatibility", "md5")
        self.hash_choice.currentIndexChanged.connect(lambda _index: self._invalidate_review())
        advanced.body_layout.addWidget(self.hash_choice)
        advanced.body_layout.addWidget(QLabel("Hash mode"))
        self.hash_mode = QComboBox()
        self.hash_mode.addItem("Full content — recommended", "full")
        self.hash_mode.addItem("Fast — sample first, then confirm", "fast")
        self.hash_mode.setToolTip(
            "Fast samples large files to shortlist candidates, then still reads every "
            "shortlisted file in full before anything is quarantined. Photos and videos "
            "that share a size are usually genuine copies, so the sample rarely rules "
            "any out and Fast ends up reading them twice. Prefer Full content unless a "
            "library has many same-size files with different contents."
        )
        self.hash_mode.currentIndexChanged.connect(lambda _index: self._invalidate_review())
        advanced.body_layout.addWidget(self.hash_mode)
        self.threads = QSpinBox()
        self.threads.setRange(1, 16)
        self.threads.setValue(4)
        self.threads.setPrefix("Hash workers: ")
        self.threads.valueChanged.connect(lambda _value: self._invalidate_review())
        advanced.body_layout.addWidget(self.threads)
        self.min_size = QSpinBox()
        self.min_size.setRange(0, 1024 * 1024)
        self.min_size.setSuffix(" KB minimum")
        self.min_size.valueChanged.connect(lambda _value: self._invalidate_review())
        advanced.body_layout.addWidget(self.min_size)
        self.exclusions = QLineEdit(", ".join(DEFAULT_EXCLUDES))
        self.exclusions.setAccessibleName("Excluded folder names")
        self.exclusions.textChanged.connect(lambda _value: self._invalidate_review())
        advanced.body_layout.addWidget(QLabel("Excluded folders"))
        advanced.body_layout.addWidget(self.exclusions)
        options_layout.addWidget(advanced)
        self.content.addWidget(options)

        self.summary_card = Card()
        summary_layout = QVBoxLayout(self.summary_card)
        summary_layout.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.XL)
        summary_layout.addWidget(SectionHeader("3. Review scan setup"))
        self.summary = QLabel("Choose a source and click Review scan setup.")
        self.summary.setWordWrap(True)
        summary_layout.addWidget(self.summary)
        self.summary_card.hide()
        self.content.addWidget(self.summary_card)

        action_row = QHBoxLayout()
        action_row.addStretch()
        self.review_button = QPushButton("Review scan setup")
        self.review_button.clicked.connect(self._review)
        self.scan_button = PrimaryButton("Run duplicate scan")
        self.scan_button.setIcon(icon("search", "#FFFFFF"))
        self.scan_button.setIconSize(icon_size())
        self.scan_button.clicked.connect(self._start_scan)
        self.scan_button.setEnabled(False)
        self.scan_button.hide()
        action_row.addWidget(self.review_button)
        action_row.addWidget(self.scan_button)
        self.content.addLayout(action_row)

        self.progress_panel = ProgressPanel()
        self.progress_panel.cancel_requested.connect(self.controller.cancel)
        self.progress_panel.hide()
        self.content.addWidget(self.progress_panel)

        self.results_card = Card()
        results_layout = QVBoxLayout(self.results_card)
        results_layout.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.XL)
        results_layout.setSpacing(Spacing.MD)
        results_layout.addWidget(
            SectionHeader(
                "5. Review duplicate results",
                "The checked rows will be moved into app-managed quarantine. The selected Keep row stays in place.",
            )
        )
        self.recoverable_label = QLabel("Estimated recoverable space: 0 B")
        self.recoverable_label.setProperty("role", "section")
        results_layout.addWidget(self.recoverable_label)
        selection_row = QHBoxLayout()
        selection_row.setSpacing(Spacing.SM)
        select_recommended = QPushButton("Select recommended copies")
        select_recommended.setToolTip("Select every copy except the file chosen to keep in each duplicate group.")
        select_recommended.clicked.connect(self._select_recommended_duplicates)
        clear_selection = QPushButton("Clear selection")
        clear_selection.clicked.connect(self._clear_duplicate_selection)
        selection_row.addWidget(select_recommended)
        selection_row.addWidget(clear_selection)
        selection_row.addStretch()
        results_layout.addLayout(selection_row)
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            ["Group", "Keep", "Quarantine", "Preview", "Filename", "Path", "Size", "Date", "Device"]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(360)
        self.table.currentCellChanged.connect(lambda *_args: self._update_duplicate_detail())
        results_layout.addWidget(self.table)
        self.duplicate_detail = QLabel("Select a duplicate row to inspect its file details.")
        self.duplicate_detail.setWordWrap(True)
        self.duplicate_detail.setProperty("muted", True)
        results_layout.addWidget(self.duplicate_detail)
        confirm_row = QHBoxLayout()
        confirm_row.addStretch()
        self.quarantine_button = PrimaryButton("Confirm quarantine")
        self.quarantine_button.setIcon(icon("quarantine", "#FFFFFF"))
        self.quarantine_button.setIconSize(icon_size())
        self.quarantine_button.clicked.connect(self._confirm_quarantine)
        confirm_row.addWidget(self.quarantine_button)
        results_layout.addLayout(confirm_row)
        self.results_card.hide()
        self.content.addWidget(self.results_card)

        self.controller.progress.connect(self._on_progress)
        self.controller.completed.connect(self._on_scan_completed)
        self.controller.cancelled.connect(self._on_scan_cancelled)
        self.controller.recoverable_error.connect(self._on_scan_error)
        self.controller.failed.connect(self._on_scan_error)
        self._apply_category_defaults()
        self.finish()

    def _apply_category_defaults(self) -> None:
        """Use the saved category preference for a new duplicate scan."""
        selected = set(self.app_settings.default_file_categories)
        for key, checkbox in (
            ("pictures", self.pictures),
            ("videos", self.videos),
            ("audio", self.audio),
            ("other", self.other),
        ):
            checkbox.setChecked(key in selected)

    def _browse(self) -> None:
        if self.source_picker.selected_key() == "android":
            self._browse_adb_path(self.path)
            return
        selected = QFileDialog.getExistingDirectory(self, "Choose scan location")
        if selected:
            self.path.set_path(selected)

    def _choose_favorite_location(self) -> None:
        location = self.favorite_location.currentData()
        if location:
            self.path.set_path(str(location))

    def update_preferences(self, settings: AppSettings) -> None:
        self.app_settings = settings
        self.favorite_location.blockSignals(True)
        self.favorite_location.clear()
        self.favorite_location.addItem("Use a saved location…", "")
        for location in settings.favorite_locations:
            self.favorite_location.addItem(location, location)
        self.favorite_location.setCurrentIndex(0)
        self.favorite_location.blockSignals(False)
        self.favorite_location.setVisible(bool(settings.favorite_locations))

    def _browse_adb_path(self, selector: PathSelector) -> None:
        from adb_bridge import ADBBridge

        serial = self.device_choice.currentData() or ""
        if not serial:
            self.path.set_error("Select an authorized Android device before browsing phone folders.")
            return
        current = ADBBridge.normalize_remote_path(selector.path() or "/sdcard")
        try:
            folders = ADBBridge.get_directory_structure(current, serial=serial)
        except Exception as exc:
            # A device error is not an empty folder. Saying so sends the user
            # looking for missing files instead of a stalled connection.
            self.banner.set_message(f"Could not list folders under {current}: {exc}", "error")
            self.banner.show()
            return
        if not folders:
            self.banner.set_message(f"No subfolders under {current}. You can type a nested path manually.", "warning")
            self.banner.show()
            return
        labels = [f"{folder['name']} — {folder['path']}" for folder in folders]
        choice, accepted = QInputDialog.getItem(
            self,
            "Choose Android folder",
            f"Folders under {current}",
            labels,
            0,
            False,
        )
        if accepted and choice:
            selector.set_path(choice.rsplit(" — ", 1)[-1])
            selector.set_error()

    def _source_changed(self, source: str) -> None:
        self._invalidate_review()
        is_android = source == "android"
        self.device_choice.setVisible(is_android)
        if is_android:
            self.path.entry.setPlaceholderText("/sdcard/DCIM")
            self.path.helper.setText("Authorize USB debugging, then choose or type an Android folder.")
            self._refresh_devices()
        else:
            self.path.entry.setPlaceholderText("Choose a folder or drive")
            self.path.helper.setText("Subfolders are included automatically.")

    def _invalidate_review(self) -> None:
        self.scan_button.setEnabled(False)
        self.scan_button.hide()

    def _refresh_devices(self) -> None:
        from adb_bridge import ADBBridge

        self.device_choice.clear()
        devices = ADBBridge.list_devices()
        for device in devices:
            label = f"{device.get('model') or device.get('serial')} — {device.get('status', 'unknown')}"
            self.device_choice.addItem(label, device.get("serial", ""))
        if not devices:
            self.device_choice.addItem("No authorized Android device found", "")
        help_text = DeviceService.connection_help(devices)
        self.path.helper.setText(
            help_text or "Authorize USB debugging, then choose or type an Android folder."
        )

    def _selected_categories(self) -> tuple[bool, list[str]]:
        extensions: list[str] = []
        if self.pictures.isChecked():
            extensions.extend([
                ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".heic", ".webp",
                ".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2",
            ])
        if self.videos.isChecked():
            extensions.extend([".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".mpeg", ".mpg", ".3gp", ".mts", ".m2ts", ".hevc"])
        if self.audio.isChecked():
            extensions.extend([".mp3", ".aac", ".m4a", ".wav", ".flac", ".ogg"])
        if self.other.isChecked():
            return False, []
        return True, sorted(set(extensions or DEFAULT_MEDIA_EXTS))

    def _build_settings(self) -> Settings:
        only_media, extensions = self._selected_categories()
        use_adb = self.source_picker.selected_key() == "android"
        self.adb_serial = self.device_choice.currentData() or ""
        excludes = [part.strip() for part in self.exclusions.text().split(",") if part.strip()]
        return Settings(
            scan_root=self.path.path(),
            output_root="",
            criteria="hash",
            hash_algo=self.hash_choice.currentData(),
            hash_mode=self.hash_mode.currentData(),
            only_media=only_media,
            extensions=extensions,
            min_size_kb=self.min_size.value(),
            exclude_dirs=excludes,
            skip_hidden_system=True,
            dry_run=True,
            preserve_structure=True,
            max_hash_workers=self.threads.value(),
            use_adb=use_adb,
            adb_serial=self.adb_serial,
        )

    def _review(self) -> None:
        if not self.path.path():
            self.path.set_error("Choose a scan location before continuing.")
            self.path.entry.setFocus()
            return
        if self.source_picker.selected_key() == "android" and not (self.device_choice.currentData() or ""):
            self.path.set_error("Select an authorized Android device before scanning.")
            return
        self.path.set_error()
        only_media, extensions = self._selected_categories()
        mode = "media categories" if only_media else "all file types"
        self.summary.setText(
            f"Source: {self.path.path()}\n"
            f"Scope: {mode}; {len(extensions) if only_media else 'all'} extension(s)\n"
            f"Hashing: {self.hash_choice.currentText()}, {self.hash_mode.currentText()}, "
            f"{self.threads.value()} worker(s)\n"
            f"Quarantine mode: {'Dry run — validate only' if self.dry_run_quarantine.isChecked() else 'Live — move selected local duplicates after confirmation'}\n"
            "Next step: run a read-only scan. Files cannot be moved until you review results and confirm quarantine."
        )
        self.summary_card.show()
        self.scan_button.setEnabled(True)
        self.scan_button.show()
        self.banner.set_message("Scan setup reviewed. You can run the read-only duplicate scan now.", "success")
        self.banner.show()

    def _refresh_review_summary(self) -> None:
        if not self.summary_card.isHidden() and self.path.path():
            self._review()

    def _start_scan(self) -> None:
        self.scan_button.setEnabled(False)
        self.review_button.setEnabled(False)
        self.results_card.hide()
        self.progress_panel.show()
        self.progress_panel.update_progress(0, "Starting scan…", "Validating source and options", "0% • 0 items • ETA —")
        started = self.controller.start(self._build_settings())
        if not started:
            self.review_button.setEnabled(True)
            self.scan_button.setEnabled(True)

    def _on_progress(self, event) -> None:
        value = int((event.progress or 0) * 100)
        detail = event.current_item or event.message
        total = event.total_items or event.total_bytes
        processed = event.processed_items or event.bytes_processed
        metrics = f"{value}%  •  {processed}/{total or '—'}  •  ETA {format_eta(event.eta_seconds)}"
        self.progress_panel.update_progress(value, event.message, detail, metrics)

    def _on_scan_completed(self, result) -> None:
        self.review_button.setEnabled(True)
        self.scan_button.setEnabled(True)
        self.progress_panel.hide()
        prefer = "quality" if self.quality.isChecked() else "newest" if self.newest.isChecked() else "oldest"
        self.review = build_duplicate_review(
            result.data.get("groups", []),
            prefer=prefer,
            thumbnail_root=self.paths.cache / "thumbnails",
            scanned_files=result.counts.get("files_scanned", 0),
            warnings=result.warnings,
        )
        self._render_review()
        if self.review.groups:
            self.banner.set_message("Scan complete. Review checked duplicates before confirming quarantine.", "success")
        else:
            self.banner.set_message("Scan complete. No duplicate groups were found.", "info")
        self.operation_service.record(
            "duplicate_scan",
            "completed",
            title="Duplicate scan",
            counts=result.counts,
            summary={
                "groups": len(self.review.groups),
                "recoverable_bytes": self.review.recoverable_size,
            },
            warnings=list(result.warnings),
        )
        self.banner.show()

    def _on_scan_cancelled(self, _result) -> None:
        self.review_button.setEnabled(True)
        self.scan_button.setEnabled(True)
        self.progress_panel.hide()
        self.banner.set_message("Scan cancelled safely. No files were moved.", "warning")
        self.banner.show()

    def _on_scan_error(self, error) -> None:
        self.review_button.setEnabled(True)
        self.scan_button.setEnabled(True)
        self.progress_panel.hide()
        self.banner.set_message(getattr(error, "message", str(error)), "danger")
        self.banner.show()

    def _render_review(self) -> None:
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(0)
        self.item_groups.clear()
        self.item_rows.clear()
        self.keep_buttons.clear()
        self.quarantine_checks.clear()
        self.keep_groups.clear()
        self._render_queue.clear()
        if not self.review or not self.review.groups:
            self.results_card.hide()
            self.table.setUpdatesEnabled(True)
            return
        for group_number, group in enumerate(self.review.groups, 1):
            keep_group = QButtonGroup(self.table)
            keep_group.setExclusive(True)
            self.keep_groups.append(keep_group)
            for item in group.items:
                self._render_queue.append((group_number, group, item))
        self.results_card.show()
        self.recoverable_label.setText("Preparing review table…")
        QTimer.singleShot(0, self._render_next_batch)

    def _render_next_batch(self) -> None:
        batch_size = 150
        self.table.setUpdatesEnabled(False)
        for _ in range(min(batch_size, len(self._render_queue))):
            group_number, group, item = self._render_queue.popleft()
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.item_groups[item.id] = group.id
            self.item_rows[item.id] = row
            self.table.setItem(row, 0, QTableWidgetItem(str(group_number)))
            keep = QRadioButton()
            keep.setChecked(item.id == group.keep_item_id)
            keep.toggled.connect(lambda checked, item_id=item.id: checked and self._keep_item(item_id))
            keep_group = self.keep_groups[group_number - 1]
            keep_group.addButton(keep)
            self.keep_buttons[item.id] = keep
            self.table.setCellWidget(row, 1, keep)
            check = QCheckBox()
            check.setChecked(item.id in group.selected_item_ids)
            # Disable the keeper's row up front. _keep_item does this, but only
            # when the user changes the keeper, so a freshly rendered review
            # left the kept file selectable for quarantine.
            check.setEnabled(item.id != group.keep_item_id)
            check.setAccessibleName(f"Quarantine {item.filename}")
            check.toggled.connect(
                lambda _checked, item_id=item.id: self._duplicate_selection_changed(item_id)
            )
            self.quarantine_checks[item.id] = check
            self.table.setCellWidget(row, 2, check)
            preview = QLabel()
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            if item.thumbnail_path:
                preview.setPixmap(QPixmap(item.thumbnail_path))
            else:
                preview.setText("No preview")
                preview.setToolTip(item.preview_status)
            self.table.setCellWidget(row, 3, preview)
            self.table.setItem(row, 4, QTableWidgetItem(item.filename))
            self.table.setItem(row, 5, QTableWidgetItem(item.path))
            self.table.setItem(row, 6, QTableWidgetItem(format_duplicate_size(item.size)))
            self.table.setItem(row, 7, QTableWidgetItem(f"{item.created_label} • {item.dimensions or 'metadata unavailable'}"))
            self.table.setItem(row, 8, QTableWidgetItem(item.device))
            self.table.setRowHeight(row, 72)
        self.table.setUpdatesEnabled(True)
        if self._render_queue:
            rendered = self.table.rowCount()
            remaining = len(self._render_queue)
            self.recoverable_label.setText(
                f"Preparing review table… {rendered} rows loaded, {remaining} remaining"
            )
            QTimer.singleShot(0, self._render_next_batch)
            return
        self._refresh_recoverable()
        if self.table.rowCount() and self.table.currentRow() < 0:
            self.table.selectRow(0)
        self._update_duplicate_detail()

    def _duplicate_selection_changed(self, item_id: str) -> None:
        row = self.item_rows.get(item_id)
        if row is not None:
            self.table.selectRow(row)
        self._refresh_recoverable()

    def _select_recommended_duplicates(self) -> None:
        """Select every copy except the one chosen to keep in each group.

        This selected every *enabled* box, and relied on the keeper's box being
        disabled to exclude it. That only happened once the user changed a
        keeper, so on a freshly rendered review the button selected the kept
        file too and confirming moved every copy of a group into quarantine.
        The keep selection is now read from the review itself.
        """

        keep_ids = {group.keep_item_id for group in (self.review.groups if self.review else ())}
        for item_id, check in self.quarantine_checks.items():
            check.setChecked(item_id not in keep_ids)
        self._refresh_recoverable()

    def _clear_duplicate_selection(self) -> None:
        for check in self.quarantine_checks.values():
            if check.isEnabled():
                check.setChecked(False)
        self._refresh_recoverable()

    def _update_duplicate_detail(self) -> None:
        if not self.review:
            self.duplicate_detail.setText("Select a duplicate row to inspect its file details.")
            return
        row = self.table.currentRow()
        item_id = next((candidate for candidate, candidate_row in self.item_rows.items() if candidate_row == row), "")
        item = next(
            (
                candidate
                for group in self.review.groups
                for candidate in group.items
                if candidate.id == item_id
            ),
            None,
        )
        if item is None:
            self.duplicate_detail.setText("Select a duplicate row to inspect its file details.")
            return
        group_id = self.item_groups.get(item.id, "")
        keep = self.keep_buttons.get(item.id)
        selected = self.quarantine_checks.get(item.id)
        action = "Keep this file" if keep and keep.isChecked() else "Quarantine selected" if selected and selected.isChecked() else "Not selected"
        self.duplicate_detail.setText(
            f"Group {group_id or '—'} • {item.filename} • {format_duplicate_size(item.size)} • "
            f"{item.path}\n{action} • {item.created_label} • {item.dimensions or 'Metadata unavailable'} • {item.device}"
        )

    def _keep_item(self, item_id: str) -> None:
        group_id = self.item_groups.get(item_id, "")
        for other_id, other_group_id in self.item_groups.items():
            if other_group_id != group_id:
                continue
            check = self.quarantine_checks.get(other_id)
            if check:
                check.blockSignals(True)
                check.setChecked(other_id != item_id)
                check.setEnabled(other_id != item_id)
                check.blockSignals(False)
        self._refresh_recoverable()
        row = self.item_rows.get(item_id)
        if row is not None:
            self.table.selectRow(row)
        self._update_duplicate_detail()

    def _apply_preference(self, prefer: str) -> None:
        if not self.review:
            return
        rebuilt = []
        for group in self.review.groups:
            ordered = sorted(group.items, key=lambda item: (item.modified, item.path.lower()))
            if prefer == "newest":
                keep = ordered[-1]
            elif prefer == "quality":
                def quality_key(item):
                    pixels = 0
                    if "×" in item.dimensions:
                        try:
                            width, height = (int(value.strip()) for value in item.dimensions.split("×", 1))
                            pixels = max(0, width) * max(0, height)
                        except ValueError:
                            pass
                    return pixels, max(0, item.size), item.modified, item.path.lower()
                keep = max(group.items, key=quality_key)
            else:
                keep = ordered[0]
            rebuilt.append(
                type(group)(
                    id=group.id,
                    hash=group.hash,
                    items=group.items,
                    keep_item_id=keep.id,
                    selected_item_ids=tuple(item.id for item in group.items if item.id != keep.id),
                )
            )
        self.review = DuplicateReview(tuple(rebuilt), self.review.scanned_files, self.review.warnings)
        self._render_review()

    def _selected_ids(self) -> list[str]:
        return [
            item_id
            for item_id, check in self.quarantine_checks.items()
            if check.isChecked() and check.isEnabled()
        ]

    def _refresh_recoverable(self) -> None:
        if not self.review:
            self.recoverable_label.setText("Estimated recoverable space: 0 B")
            return
        sizes = {
            item.id: item.size
            for group in self.review.groups
            for item in group.items
        }
        selected = self._selected_ids()
        total = sum(sizes.get(item_id, 0) for item_id in selected)
        self.recoverable_label.setText(
            f"Estimated recoverable space: {format_duplicate_size(total)} across {len(selected)} file(s)"
        )
        self.quarantine_button.setEnabled(bool(selected))

    def _confirm_quarantine(self) -> None:
        if not self.review:
            return
        selected = self._selected_ids()
        if not selected:
            self.banner.set_message("Choose at least one duplicate to quarantine.", "warning")
            self.banner.show()
            return
        if self.dry_run_quarantine.isChecked():
            title = "Dry run quarantine"
            message = (
                "Validate the checked duplicates and write a quarantine manifest without moving local files "
                "or pulling Android copies?"
            )
        else:
            title = "Confirm quarantine"
            message = (
                "Move the checked local duplicates into app-managed quarantine?\n\n"
                "Android files are copied into quarantine; phone originals are left untouched."
            )
        response = QMessageBox.question(self, title, message)
        if response != QMessageBox.StandardButton.Yes:
            return
        review = self.review
        adb_serial = self.adb_serial
        dry_run = self.dry_run_quarantine.isChecked()
        self.quarantine_button.setEnabled(False)
        self.banner.set_message("Quarantining selected duplicates…", "info")
        self.banner.show()
        self.run_in_background(
            lambda: self.quarantine_service.quarantine(
                review, selected, adb_serial=adb_serial, dry_run=dry_run
            ),
            self._quarantine_finished,
            self._quarantine_failed,
        )

    def _quarantine_failed(self, error) -> None:
        self.quarantine_button.setEnabled(True)
        self.banner.set_message(f"Quarantine failed: {getattr(error, 'message', error)}", "error")
        self.banner.show()

    def _quarantine_finished(self, result) -> None:
        self.quarantine_button.setEnabled(True)
        self.operation_service.record(
            "duplicate_quarantine",
            "warning" if result.failures else "completed",
            title="Duplicate quarantine",
            counts={"quarantined": result.quarantined_count, "failures": len(result.failures)},
            summary={"manifest_path": result.manifest_path},
            failures=list(result.failures),
        )
        if result.dry_run:
            self.banner.set_message(
                f"Dry run complete: {result.quarantined_count} file(s) validated, {len(result.failures)} issue(s).",
                "info" if not result.failures else "warning",
            )
        elif result.failures:
            self.banner.set_message(
                f"Quarantined {result.quarantined_count} file(s), with {len(result.failures)} item(s) needing attention.",
                "warning",
            )
        else:
            self.banner.set_message(
                f"Quarantined {result.quarantined_count} duplicate file(s). Manifest: {result.manifest_path}",
                "success",
            )
        self.banner.show()


class ImportPage(BasePage):
    def __init__(
        self,
        hash_cache: HashCache | None = None,
        operation_service: OperationRecordService | None = None,
        settings: AppSettings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "Import Files",
            "Import only new files after comparing a source against your existing library.",
            parent,
        )
        self.paths = get_runtime_paths()
        self.hash_cache = hash_cache or HashCache(str(self.paths.hash_cache))
        if hash_cache is None:
            self.hash_cache.load()
        self.operation_service = operation_service or OperationRecordService(self.paths)
        self.app_settings = settings or AppSettings()
        self.controller = TransferController(self.hash_cache)
        self.current_settings = None
        self.activity_lines: list[str] = []
        self.adb_serial = ""

        self.content.addWidget(
            StepIndicator(["Source", "Library", "Save", "File types", "Review", "Run", "Result"])
        )
        self.banner = ToastBanner(
            "Choose the source and library, then review the copy-only import before running it.",
            "info",
        )
        self.banner.hide()
        self.content.addWidget(self.banner)

        source = Card()
        source_layout = QVBoxLayout(source)
        source_layout.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.XL)
        source_layout.setSpacing(Spacing.LG)
        source_layout.addWidget(SectionHeader("1. Choose an import source"))
        self.source_picker = SourcePicker(
            [
                ("phone", "Android phone", "USB connection", "phone"),
                ("folder", "Folder", "On this PC", "folder"),
                ("drive", "External drive", "USB or storage", "drive"),
            ]
        )
        self.source_picker.select("folder")
        self.source_picker.selection_changed.connect(self._source_changed)
        self.device_choice = QComboBox()
        self.device_choice.setAccessibleName("Android device")
        self.source_path = PathSelector(
            "Import from",
            "Choose a folder or drive",
            "Source files remain unchanged.",
        )
        self.source_path.path_changed.connect(lambda _value: self._invalidate_review())
        self.source_path.browse_requested.connect(
            lambda: self._browse(self.source_path, "Choose import source")
        )
        source_layout.addWidget(self.source_picker)
        source_layout.addWidget(self.device_choice)
        source_layout.addWidget(self.source_path)
        self.favorite_source = QComboBox()
        self.favorite_source.setAccessibleName("Saved import source locations")
        self.favorite_source.addItem("Use a saved location…", "")
        for location in self.app_settings.favorite_locations:
            self.favorite_source.addItem(location, location)
        self.favorite_source.currentIndexChanged.connect(self._choose_favorite_source)
        self.favorite_source.setVisible(bool(self.app_settings.favorite_locations))
        source_layout.addWidget(self.favorite_source)
        # Sourced from the service rather than hardcoded so enabling iOS later
        # is a service change, not a hunt for strings scattered through the UI.
        ios_status = IOSTransferService().status()
        if not ios_status.get("supported"):
            source_layout.addWidget(InlineMessage(str(ios_status.get("message", "")), "info"))
        self.content.addWidget(source)

        destination = Card()
        destination_layout = QVBoxLayout(destination)
        destination_layout.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.XL)
        destination_layout.setSpacing(Spacing.LG)
        destination_layout.addWidget(
            SectionHeader(
                "2. Choose your existing library",
                "The existing library is searched for duplicates before anything is copied.",
            )
        )
        self.library_path = PathSelector(
            "Existing library",
            "Choose the folder containing your current files",
        )
        self.library_path.path_changed.connect(lambda _value: self._invalidate_review())
        self.output_path = PathSelector(
            "Save new files to",
            "Leave blank to save into the existing library",
            "When blank, new files are copied into the existing library while preserving source folders.",
        )
        self.output_path.path_changed.connect(lambda _value: self._invalidate_review())
        self.library_path.browse_requested.connect(
            lambda: self._browse(self.library_path, "Choose existing library")
        )
        self.output_path.browse_requested.connect(
            lambda: self._browse(self.output_path, "Choose save location")
        )
        destination_layout.addWidget(self.library_path)
        self.favorite_library = QComboBox()
        self.favorite_library.setAccessibleName("Saved existing library locations")
        self.favorite_library.addItem("Use a saved location…", "")
        for location in self.app_settings.favorite_locations:
            self.favorite_library.addItem(location, location)
        self.favorite_library.currentIndexChanged.connect(self._choose_favorite_library)
        self.favorite_library.setVisible(bool(self.app_settings.favorite_locations))
        destination_layout.addWidget(self.favorite_library)
        destination_layout.addWidget(SectionHeader("3. Choose where new files should be saved"))
        destination_layout.addWidget(self.output_path)
        self.same_location_message = InlineMessage(
            "If left blank, the existing library and save location are the same. "
            "Duplicate & Transfer Manager will still compare first, then copy only new files.",
            "info",
        )
        destination_layout.addWidget(self.same_location_message)
        self.destination_template = QComboBox()
        self.destination_template.addItem("Preserve source folders — recommended", "preserve")
        self.destination_template.addItem("Organize into date folders — YYYY/MM", "date")
        self.destination_template.setAccessibleName("Destination organization")
        self.destination_template.setToolTip("Date folders use each source file's available timestamp. This never changes source files.")
        self.destination_template.currentIndexChanged.connect(lambda _index: self._invalidate_review())
        destination_layout.addWidget(QLabel("Organize imported files"))
        destination_layout.addWidget(self.destination_template)
        self.content.addWidget(destination)

        file_types = Card()
        type_layout = QVBoxLayout(file_types)
        type_layout.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.XL)
        type_layout.setSpacing(Spacing.MD)
        type_layout.addWidget(SectionHeader("4. Choose file types"))
        type_row = QHBoxLayout()
        self.category_checks: dict[str, QCheckBox] = {}
        for key, label, checked in (
            ("pictures", "Pictures", True),
            ("videos", "Videos", True),
            ("audio", "Audio", False),
            ("documents", "Documents", False),
            ("other", "Other file types", False),
        ):
            option = QCheckBox(label)
            option.setChecked(
                key in self.app_settings.default_file_categories
                if self.app_settings.default_file_categories
                else checked
            )
            option.toggled.connect(lambda _checked: self._invalidate_review())
            self.category_checks[key] = option
            type_row.addWidget(option)
        type_row.addStretch()
        type_layout.addLayout(type_row)

        self.profile = QComboBox()
        for name, values in TRANSFER_PROFILES.items():
            suffix = " — recommended" if name == "Balanced" else ""
            self.profile.addItem(f"{name}{suffix}", name)
        default_profile_index = self.profile.findData(self.app_settings.default_transfer_profile)
        self.profile.setCurrentIndex(max(0, default_profile_index))
        self.profile.currentIndexChanged.connect(self._profile_changed)
        self.profile.currentIndexChanged.connect(lambda _index: self._invalidate_review())
        self.profile_description = QLabel(
            TRANSFER_PROFILES[self.profile.currentData() or "Balanced"]["description"]
        )
        self.profile_description.setProperty("muted", True)
        self.profile_description.setWordWrap(True)
        type_layout.addWidget(QLabel("Transfer profile"))
        type_layout.addWidget(self.profile)
        type_layout.addWidget(self.profile_description)

        advanced = DisclosurePanel()
        self.hash_mode = QComboBox()
        self.hash_mode.addItem("Use profile default", "")
        self.hash_mode.addItem("Full content", "full")
        self.hash_mode.addItem("Fast large-file sampling", "fast")
        self.hash_mode.setToolTip(
            "Fast samples the start and end of large files instead of reading them "
            "whole. It helps least on photos and videos, where files of the same size "
            "are usually genuine copies."
        )
        self.hash_mode.currentIndexChanged.connect(lambda _index: self._invalidate_review())
        self.worker_count = QSpinBox()
        self.worker_count.setRange(0, 16)
        self.worker_count.setSpecialValueText("Profile default")
        self.worker_count.valueChanged.connect(lambda _value: self._invalidate_review())
        self.retry_count = QSpinBox()
        self.retry_count.setRange(0, 10)
        self.retry_count.setSpecialValueText("Profile default")
        self.retry_count.valueChanged.connect(lambda _value: self._invalidate_review())
        self.conflict = QComboBox()
        self.conflict.addItem("Rename if a filename exists", "rename")
        self.conflict.addItem("Skip existing filename", "skip")
        self.conflict.addItem("Replace existing filename", "replace")
        self.conflict.currentIndexChanged.connect(lambda _index: self._invalidate_review())
        self.use_cache = QCheckBox("Use existing library cache")
        self.use_cache.setChecked(True)
        self.update_cache = QCheckBox("Update caches after successful copy")
        self.update_cache.setChecked(True)
        self.dry_run = QCheckBox("Dry run — review what would copy without writing files")
        self.dry_run.setChecked(False)
        self.dry_run_cleanup = QCheckBox("Dry run partial cleanup")
        self.dry_run_cleanup.setToolTip("List partial files that would be removed without deleting them.")
        self.use_adb_cache = QCheckBox("Use Android hash cache")
        self.use_adb_cache.setChecked(True)
        self.keep_awake = QCheckBox("Keep Android awake during transfer")
        self.keep_awake.setChecked(True)
        self.verify_resumed = QCheckBox("Re-read every resumed file to verify it")
        self.verify_resumed.setChecked(False)
        self.verify_resumed.setToolTip(
            "Resuming normally trusts files whose size and timestamp still match the "
            "journal. Turn this on to hash every already-copied file again. It detects "
            "silent corruption but re-reads the whole destination, which is slow on "
            "large libraries."
        )
        self.reconnect_timeout = QSpinBox()
        self.reconnect_timeout.setRange(30, 3600)
        self.reconnect_timeout.setValue(300)
        self.reconnect_timeout.setSuffix(" sec reconnect timeout")
        self.reconnect_timeout.valueChanged.connect(lambda _value: self._invalidate_review())
        self.stall_timeout = QSpinBox()
        self.stall_timeout.setRange(30, 1800)
        self.stall_timeout.setValue(180)
        self.stall_timeout.setSuffix(" sec stall timeout")
        self.stall_timeout.valueChanged.connect(lambda _value: self._invalidate_review())
        cleanup_partials = QPushButton("Clean partial files in save location")
        cleanup_partials.clicked.connect(self._cleanup_partials)
        for label, widget in (
            ("Hash mode", self.hash_mode),
            ("Worker count", self.worker_count),
            ("Retries", self.retry_count),
            ("Conflict policy", self.conflict),
        ):
            advanced.body_layout.addWidget(QLabel(label))
            advanced.body_layout.addWidget(widget)
        for widget in (self.dry_run, self.dry_run_cleanup, self.use_cache, self.update_cache, self.use_adb_cache, self.keep_awake, self.verify_resumed):
            widget.toggled.connect(lambda _checked: self._invalidate_review())
            advanced.body_layout.addWidget(widget)
        advanced.body_layout.addWidget(self.reconnect_timeout)
        advanced.body_layout.addWidget(self.stall_timeout)
        advanced.body_layout.addWidget(cleanup_partials)
        type_layout.addWidget(advanced)
        self.content.addWidget(file_types)

        self.content.addWidget(
            InlineMessage(
                "Imports are copy-only and structure-preserving by default. Source files are not modified. "
                "Pause/resume checkpoints are saved between completed files; full live pause controls are planned.",
                "success",
            )
        )

        self.review_card = CompletionSummary("Review import operation")
        self.review_card.hide()
        self.content.addWidget(self.review_card)

        action = QHBoxLayout()
        action.addStretch()
        self.review_button = QPushButton("Review import setup")
        self.review_button.clicked.connect(self._review)
        self.run_button = PrimaryButton("Run import")
        self.run_button.setIcon(icon("import", "#FFFFFF"))
        self.run_button.setIconSize(icon_size())
        self.run_button.setEnabled(False)
        self.run_button.hide()
        self.run_button.clicked.connect(self._run_import)
        action.addWidget(self.review_button)
        action.addWidget(self.run_button)
        self.content.addLayout(action)

        self.progress_panel = ProgressPanel()
        self.progress_panel.cancel_requested.connect(self.controller.cancel)
        self.progress_panel.hide()
        self.content.addWidget(self.progress_panel)

        self.stage_card = Card()
        stage_layout = QVBoxLayout(self.stage_card)
        stage_layout.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.XL)
        stage_layout.addWidget(SectionHeader("Live stages"))
        self.stage_labels: dict[str, QLabel] = {}
        stage_grid = QGridLayout()
        for index, (key, label) in enumerate(STAGE_LABELS.items()):
            marker = QLabel(f"○ {label}")
            marker.setProperty("muted", True)
            self.stage_labels[key] = marker
            stage_grid.addWidget(marker, index // 3, index % 3)
        stage_layout.addLayout(stage_grid)
        self.stage_card.hide()
        self.content.addWidget(self.stage_card)

        self.result_summary = CompletionSummary("Import result")
        self.result_summary.hide()
        self.content.addWidget(self.result_summary)

        self.log_panel = DisclosurePanel("Detailed activity log")
        self.activity_log = QTextEdit()
        self.activity_log.setReadOnly(True)
        self.activity_log.setMinimumHeight(160)
        self.activity_log.setAccessibleName("Detailed import activity log")
        self.log_panel.body_layout.addWidget(self.activity_log)
        self.log_panel.hide()
        self.content.addWidget(self.log_panel)

        self.controller.progress.connect(self._on_progress)
        self.controller.completed.connect(self._on_completed)
        self.controller.cancelled.connect(self._on_cancelled)
        self.controller.recoverable_error.connect(self._on_failed)
        self.controller.failed.connect(self._on_failed)
        self.controller.technical_log.connect(self._on_log)
        self._source_changed("folder")
        self.finish()

    def _browse(self, selector: PathSelector, title: str) -> None:
        if selector is self.source_path and self.source_picker.selected_key() == "phone":
            self._browse_adb_path(selector)
            return
        selected = QFileDialog.getExistingDirectory(self, title)
        if selected:
            selector.set_path(selected)

    def _choose_favorite_source(self) -> None:
        location = self.favorite_source.currentData()
        if location:
            self.source_path.set_path(str(location))

    def _choose_favorite_library(self) -> None:
        location = self.favorite_library.currentData()
        if location:
            self.library_path.set_path(str(location))

    def update_preferences(self, settings: AppSettings) -> None:
        self.app_settings = settings
        for combo in (self.favorite_source, self.favorite_library):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("Use a saved location…", "")
            for location in settings.favorite_locations:
                combo.addItem(location, location)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
            combo.setVisible(bool(settings.favorite_locations))

    def _browse_adb_path(self, selector: PathSelector) -> None:
        from adb_bridge import ADBBridge

        serial = self.device_choice.currentData() or ""
        if not serial:
            selector.set_error("Select an authorized Android device before browsing phone folders.")
            return
        current = ADBBridge.normalize_remote_path(selector.path() or "/sdcard")
        try:
            folders = ADBBridge.get_directory_structure(current, serial=serial)
        except Exception as exc:
            # A device error is not an empty folder. Saying so sends the user
            # looking for missing files instead of a stalled connection.
            self.banner.set_message(f"Could not list folders under {current}: {exc}", "error")
            self.banner.show()
            return
        if not folders:
            self.banner.set_message(f"No subfolders under {current}. You can type a nested path manually.", "warning")
            self.banner.show()
            return
        labels = [f"{folder['name']} — {folder['path']}" for folder in folders]
        choice, accepted = QInputDialog.getItem(
            self,
            "Choose Android folder",
            f"Folders under {current}",
            labels,
            0,
            False,
        )
        if accepted and choice:
            selector.set_path(choice.rsplit(" — ", 1)[-1])
            selector.set_error()

    def _source_changed(self, source: str) -> None:
        self._invalidate_review()
        is_phone = source == "phone"
        self.device_choice.setVisible(is_phone)
        self.use_adb_cache.setVisible(is_phone)
        self.keep_awake.setVisible(is_phone)
        if is_phone:
            self.source_path.entry.setPlaceholderText("/sdcard/DCIM")
            self._refresh_devices()
        else:
            self.source_path.entry.setPlaceholderText("Choose a folder or drive")

    def _invalidate_review(self) -> None:
        self.current_settings = None
        self.run_button.setEnabled(False)
        self.run_button.hide()

    def _refresh_devices(self) -> None:
        from adb_bridge import ADBBridge

        self.device_choice.clear()
        devices = ADBBridge.list_devices()
        for device in devices:
            label = f"{device.get('model') or device.get('serial')} — {device.get('status', 'unknown')}"
            self.device_choice.addItem(label, device.get("serial", ""))
        if not devices:
            self.device_choice.addItem("No authorized Android device found", "")
        help_text = DeviceService.connection_help(devices)
        if help_text:
            self.banner.set_message(help_text, "warning")
            self.banner.show()
        elif self.banner.isVisible():
            self.banner.hide()

    def _profile_changed(self) -> None:
        profile = self.profile.currentData() or "Balanced"
        self.profile_description.setText(TRANSFER_PROFILES[profile]["description"])

    def _selected_categories(self) -> list[str]:
        return [key for key, check in self.category_checks.items() if check.isChecked()]

    def _build_settings(self):
        profile = self.profile.currentData() or "Balanced"
        self.adb_serial = self.device_choice.currentData() or ""
        return build_import_settings(
            source_root=self.source_path.path(),
            existing_library=self.library_path.path(),
            save_to=self.output_path.path(),
            source_kind=self.source_picker.selected_key(),
            categories=self._selected_categories(),
            profile=profile,
            hash_mode=self.hash_mode.currentData() or None,
            max_hash_workers=self.worker_count.value() or None,
            retry_attempts=self.retry_count.value() or None,
            conflict_policy=self.conflict.currentData(),
            use_dest_cache=self.use_cache.isChecked(),
            update_drive_cache=self.update_cache.isChecked(),
            use_adb_cache=self.use_adb_cache.isChecked(),
            keep_device_awake=self.keep_awake.isChecked(),
            adb_serial=self.adb_serial,
            reconnect_timeout=self.reconnect_timeout.value(),
            stall_timeout=self.stall_timeout.value(),
            destination_template=self.destination_template.currentData(),
            verify_resumed_files=self.verify_resumed.isChecked(),
            dry_run=self.dry_run.isChecked(),
        )

    def _resume_setup(self) -> dict:
        """Capture only the user-visible setup needed for a reviewed journal resume."""
        return {
            "source_kind": self.source_picker.selected_key(),
            "source_root": self.source_path.path(),
            "existing_library": self.library_path.path(),
            "save_to": self.output_path.path(),
            "destination_template": self.destination_template.currentData() or "preserve",
            "categories": self._selected_categories(),
            "profile": self.profile.currentData() or "Balanced",
            "hash_mode": self.hash_mode.currentData() or "",
            "worker_count": self.worker_count.value(),
            "retry_count": self.retry_count.value(),
            "conflict": self.conflict.currentData() or "rename",
            "use_cache": self.use_cache.isChecked(),
            "update_cache": self.update_cache.isChecked(),
            "use_adb_cache": self.use_adb_cache.isChecked(),
            "keep_awake": self.keep_awake.isChecked(),
            "verify_resumed": self.verify_resumed.isChecked(),
            "reconnect_timeout": self.reconnect_timeout.value(),
            "stall_timeout": self.stall_timeout.value(),
            "dry_run": self.dry_run.isChecked(),
            "adb_serial": self.adb_serial,
        }

    def apply_resume_setup(self, setup: dict) -> None:
        """Restore a cancelled import's setup but require a fresh user review."""
        source_kind = str(setup.get("source_kind", "folder"))
        if source_kind in self.source_picker.cards:
            self.source_picker.select(source_kind)
        self.source_path.set_path(str(setup.get("source_root", "")))
        self.library_path.set_path(str(setup.get("existing_library", "")))
        self.output_path.set_path(str(setup.get("save_to", "")))
        template_index = self.destination_template.findData(str(setup.get("destination_template", "preserve")))
        if template_index >= 0:
            self.destination_template.setCurrentIndex(template_index)
        selected_categories = set(setup.get("categories", []))
        for key, check in self.category_checks.items():
            check.setChecked(key in selected_categories)
        profile_index = self.profile.findData(str(setup.get("profile", "Balanced")))
        if profile_index >= 0:
            self.profile.setCurrentIndex(profile_index)
        for choice, value in ((self.hash_mode, setup.get("hash_mode", "")), (self.conflict, setup.get("conflict", "rename"))):
            index = choice.findData(value)
            if index >= 0:
                choice.setCurrentIndex(index)
        self.worker_count.setValue(int(setup.get("worker_count", 0) or 0))
        self.retry_count.setValue(int(setup.get("retry_count", 0) or 0))
        self.use_cache.setChecked(bool(setup.get("use_cache", True)))
        self.update_cache.setChecked(bool(setup.get("update_cache", True)))
        self.use_adb_cache.setChecked(bool(setup.get("use_adb_cache", True)))
        self.keep_awake.setChecked(bool(setup.get("keep_awake", True)))
        self.verify_resumed.setChecked(bool(setup.get("verify_resumed", False)))
        self.reconnect_timeout.setValue(int(setup.get("reconnect_timeout", 300) or 300))
        self.stall_timeout.setValue(int(setup.get("stall_timeout", 180) or 180))
        self.dry_run.setChecked(bool(setup.get("dry_run", False)))
        serial = str(setup.get("adb_serial", ""))
        if source_kind == "phone" and serial:
            index = self.device_choice.findData(serial)
            if index < 0:
                self.device_choice.addItem(f"Reconnect previous Android device — {serial}", serial)
                index = self.device_choice.findData(serial)
            self.device_choice.setCurrentIndex(index)
        self._invalidate_review()
        self.banner.set_message(
            "Previous import setup restored. Confirm the source and destination, then review before resuming.",
            "info",
        )
        self.banner.show()

    def _review(self) -> None:
        valid = True
        if not self.source_path.path():
            self.source_path.set_error("Choose an import source.")
            valid = False
        else:
            self.source_path.set_error()
        if not self.library_path.path():
            self.library_path.set_error("Choose your existing library.")
            valid = False
        else:
            self.library_path.set_error()
        if not valid:
            self.source_path.entry.setFocus()
            return
        self.current_settings = self._build_settings()
        source_label = self.source_picker.group.checkedButton().accessibleName()
        review = build_import_review(
            source_label=source_label,
            source_root=self.source_path.path(),
            existing_library=self.library_path.path(),
            save_to=self.output_path.path(),
            categories=self._selected_categories(),
            profile=self.profile.currentData() or "Balanced",
            settings=self.current_settings,
        )
        same_text = (
            "Existing library and save location are the same; new files will be copied into the library after comparison."
            if review.same_library_and_save
            else "New files will be copied into the separate save location you selected."
        )
        self.review_card.set_metrics(
            [
                ("Source", f"{review.source_label}: {review.source_root}"),
                ("Existing library", review.existing_library),
                ("Save new files to", review.save_to),
                ("Copy mode", "Copy-only, structure-preserving"),
                ("Dry run", "Yes — no files will be written" if self.current_settings.dry_run else "No — copy only new files after comparison"),
                ("Profile", f"{review.profile}: {review.profile_description}"),
                ("Advanced", review.advanced_summary),
                ("Location note", same_text),
            ]
        )
        self.review_card.show()
        self.run_button.setEnabled(True)
        self.run_button.show()
        self.banner.set_message("Import reviewed. Run it when you are ready.", "success")
        self.banner.show()

    def _run_import(self) -> None:
        if self.current_settings is None:
            self._review()
        if self.current_settings is None:
            return
        self.activity_lines.clear()
        self.activity_log.clear()
        self.log_panel.show()
        self.result_summary.hide()
        self.stage_card.show()
        self.progress_panel.show()
        self.review_button.setEnabled(False)
        self.run_button.setEnabled(False)
        self._set_stage("validation")
        self.progress_panel.update_progress(0, "Validating transfer settings…", "Preparing import", "0% • 0 items • ETA —")
        if not self.controller.start(self.current_settings):
            self.review_button.setEnabled(True)
            self.run_button.setEnabled(True)

    def _cleanup_partials(self) -> None:
        root = self.output_path.path() or self.library_path.path()
        if not root:
            self.banner.set_message("Choose an existing library or save location before cleaning partial files.", "warning")
            self.banner.show()
            return
        if not self.dry_run_cleanup.isChecked():
            response = QMessageBox.question(
                self,
                "Clean partial files",
                "Remove only app-owned temporary transfer files recorded in the transfer journal "
                "or stored in Duplicate & Transfer Manager's staging folder?\n\n"
                "Files are not selected by extension, so unrelated user files are left untouched.",
            )
            if response != QMessageBox.StandardButton.Yes:
                return
        removed = cleanup_partial_files(root, dry_run=self.dry_run_cleanup.isChecked())
        action = "Would remove" if self.dry_run_cleanup.isChecked() else "Removed"
        self.banner.set_message(f"{action} {len(removed)} partial transfer file(s).", "success")
        self.banner.show()

    def _set_stage(self, active_stage: str) -> None:
        for key, label in self.stage_labels.items():
            active = key == active_stage
            label.setText(f"{'●' if active else '○'} {STAGE_LABELS[key]}")
            label.setProperty("muted", not active)
            label.style().unpolish(label)
            label.style().polish(label)

    def _on_progress(self, event) -> None:
        stage = event.phase.value
        if stage == "transfer":
            stage = "transfer"
        elif stage not in self.stage_labels:
            stage = "transfer"
        self._set_stage(stage)
        value = int((event.progress or 0) * 100)
        processed = event.processed_items or event.bytes_processed
        total = event.total_items or event.total_bytes
        metrics = f"{value}%  •  {processed}/{total or '—'}  •  ETA {format_eta(event.eta_seconds)}"
        self.progress_panel.update_progress(value, event.message, event.current_item or event.message, metrics)

    def _on_log(self, line: str) -> None:
        if not line:
            return
        self.activity_lines.append(line)
        self.activity_log.setPlainText("\n".join(self.activity_lines[-500:]))
        self.activity_log.moveCursor(self.activity_log.textCursor().MoveOperation.End)

    def _on_completed(self, result) -> None:
        self.review_button.setEnabled(True)
        self.run_button.setEnabled(True)
        self.progress_panel.hide()
        self._set_stage("finalization")
        summary = summarize_transfer_result(result)
        self.result_summary.set_metrics(summary.items())
        self.result_summary.show()
        self.operation_service.record(
            "import",
            "completed" if result.counts.get("errors", 0) == 0 else "warning",
            title="File import",
            counts=result.counts,
            summary=summary,
            report_path=result.report_path,
            resume_available=bool(result.resume_information),
            failures=[failure.message for failure in result.failures],
        )
        self.banner.set_message("Import complete. Review the summary cards and detailed activity log.", "success")
        self.banner.show()

    def _on_cancelled(self, _result) -> None:
        self.review_button.setEnabled(True)
        self.run_button.setEnabled(True)
        self.progress_panel.hide()
        self.operation_service.record(
            "import",
            "cancelled",
            title="Cancelled import",
            summary={
                "resume": "Completed files remain recorded for resume.",
                "resume_setup": self._resume_setup(),
            },
            resume_available=True,
        )
        self.banner.set_message("Import cancelled safely. Completed files remain recorded for resume.", "warning")
        self.banner.show()

    def _on_failed(self, error) -> None:
        self.review_button.setEnabled(True)
        self.run_button.setEnabled(True)
        self.progress_panel.hide()
        self.operation_service.record(
            "import",
            "failed",
            title="Failed import",
            summary={"resume_setup": self._resume_setup()},
            failures=[getattr(error, "message", str(error))],
            resume_available=True,
        )
        self.banner.set_message(getattr(error, "message", str(error)), "danger")
        self.banner.show()


class ActivityPage(BasePage):
    navigate_requested = Signal(str)
    resume_requested = Signal(object)

    def __init__(
        self,
        operations: OperationRecordService | None = None,
        reports: ReportService | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "Activity",
            "Review completed operations, warnings, and detailed local reports.",
            parent,
        )
        self.operations = operations or OperationRecordService()
        self.reports = reports or ReportService()
        self.records: list[dict] = []

        self.summary_grid = ResponsiveGrid(
            [
                MetricCard("0", "Visible records"),
                MetricCard("0", "Warnings or failures"),
                MetricCard("0", "Reports available"),
            ],
            min_column_width=260,
            max_columns=3,
        )
        self.content.addWidget(self.summary_grid)

        self.content.addWidget(
            InlineMessage(
                "Activity is stored locally. Reports can be opened, exported, or removed without changing imported files.",
                "success",
            )
        )

        toolbar_card = Card(subtle=True)
        toolbar = QVBoxLayout(toolbar_card)
        toolbar.setContentsMargins(Spacing.LG, Spacing.MD, Spacing.LG, Spacing.MD)
        toolbar.setSpacing(Spacing.SM)
        filter_row = QHBoxLayout()
        filter_row.setSpacing(Spacing.MD)
        action_row = QHBoxLayout()
        action_row.setSpacing(Spacing.MD)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search activity")
        self.search.setAccessibleName("Search activity")
        self.search.setMinimumWidth(260)
        self.search.textChanged.connect(self.refresh)
        self.filter_choice = QComboBox()
        self.filter_choice.addItems(["All operations", "Duplicate scans", "Imports", "Sorting", "Warnings", "Reports", "Audit history"])
        self.filter_choice.setMinimumWidth(170)
        self.filter_choice.currentIndexChanged.connect(self.refresh)
        self.dry_run_reports = QCheckBox("Dry run report actions")
        self.dry_run_reports.setToolTip("Preview export/remove report actions without writing or deleting files.")
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        open_report = QPushButton("Open report")
        open_report.clicked.connect(self.open_selected_report)
        open_reports_folder = QPushButton("Open reports folder")
        open_reports_folder.clicked.connect(self.open_reports_folder)
        export_report = QPushButton("Export report")
        export_report.clicked.connect(self.export_selected_report)
        export_activity = QPushButton("Export activity CSV")
        export_activity.setToolTip("Export checked activity rows, or all currently visible rows, without local file paths.")
        export_activity.clicked.connect(self.export_activity_csv)
        resume_import = QPushButton("Resume import setup")
        resume_import.setToolTip("Restore a cancelled import setup for review. The import will not start automatically.")
        resume_import.clicked.connect(self.resume_selected_import)
        remove_report = QPushButton("Remove report")
        remove_report.clicked.connect(self.remove_selected_report)
        filter_row.addWidget(QLabel("Find"))
        filter_row.addWidget(self.search, 1)
        filter_row.addWidget(QLabel("Show"))
        filter_row.addWidget(self.filter_choice)
        filter_row.addWidget(self.dry_run_reports)
        action_row.addStretch()
        action_row.addWidget(refresh)
        action_row.addWidget(open_report)
        action_row.addWidget(open_reports_folder)
        action_row.addWidget(export_report)
        action_row.addWidget(export_activity)
        action_row.addWidget(resume_import)
        action_row.addWidget(remove_report)
        toolbar.addLayout(filter_row)
        toolbar.addLayout(action_row)
        self.content.addWidget(toolbar_card)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Select", "Type", "Status", "Created", "Title", "Counts"]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(0, 72)
        self.table.setMinimumHeight(340)
        self.table.currentCellChanged.connect(lambda *_args: self._update_details())
        self.content.addWidget(self.table)

        self.detail_card = Card(subtle=True)
        self.detail_card.setMaximumHeight(190)
        detail_layout = QVBoxLayout(self.detail_card)
        detail_layout.setContentsMargins(Spacing.LG, Spacing.MD, Spacing.LG, Spacing.MD)
        detail_layout.setSpacing(Spacing.SM)
        self.detail_title = QLabel("No activity selected")
        self.detail_title.setProperty("role", "section")
        self.detail_text = QLabel("Select an activity row to see report path, warnings, failures, and record details.")
        self.detail_text.setWordWrap(True)
        self.detail_text.setProperty("muted", True)
        detail_layout.addWidget(self.detail_title)
        detail_layout.addWidget(self.detail_text)
        self.content.addWidget(self.detail_card)

        self.empty = EmptyState(
            "No activity yet",
            "Completed scans and imports will appear here with their reports and outcomes.",
            "Start an import",
            "activity",
        )
        self.empty.action_requested.connect(lambda: self.navigate_requested.emit("import"))
        self.content.addWidget(self.empty)
        self.banner = ToastBanner("Activity actions appear here.", "info")
        self.banner.hide()
        self.content.addWidget(self.banner)
        self.refresh()
        self.finish()

    def refresh(self) -> None:
        query = self.search.text().lower().strip()
        selected_filter = self.filter_choice.currentText()
        self.records = []
        source_records = (
            self.operations.list_audit_events()
            if selected_filter == "Audit history"
            else self.operations.list_records()
        )
        for record in source_records:
            if selected_filter == "Duplicate scans" and record.get("type") != "duplicate_scan":
                continue
            if selected_filter == "Imports" and record.get("type") != "import":
                continue
            if selected_filter == "Sorting" and not (
                str(record.get("type", "")).startswith("sorting")
                or str(record.get("type", "")) == "scheduled_sort"
                or str(record.get("type", "")).startswith("organization")
            ):
                continue
            if selected_filter == "Warnings" and record.get("status") not in {"warning", "failed"}:
                continue
            if selected_filter == "Reports" and not record.get("report_path"):
                continue
            haystack = " ".join(str(record.get(key, "")) for key in ("type", "status", "title", "created_at", "report_path")).lower()
            if query and query not in haystack:
                continue
            self.records.append(record)
        self.table.setRowCount(0)
        for row, record in enumerate(self.records):
            self.table.insertRow(row)
            check = QCheckBox()
            check.setAccessibleName(f"Select activity {record.get('title', record.get('id', row))}")
            check.toggled.connect(lambda checked, target_row=row: self._activity_selection_changed(target_row, checked))
            self.table.setCellWidget(row, 0, check)
            for column, value in enumerate(
                [
                    record.get("type", ""),
                    record.get("status", ""),
                    record.get("created_at", "")[:19],
                    record.get("title", ""),
                    self._counts_text(record),
                ],
                start=1,
            ):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        warnings = sum(1 for record in self.records if record.get("status") in {"warning", "failed"} or record.get("warnings") or record.get("failures"))
        reports = sum(1 for record in self.records if record.get("report_path"))
        self.summary_grid.widgets[0].layout().itemAt(0).widget().setText(str(len(self.records)))
        self.summary_grid.widgets[1].layout().itemAt(0).widget().setText(str(warnings))
        self.summary_grid.widgets[2].layout().itemAt(0).widget().setText(str(reports))
        self.empty.setVisible(not self.records)
        self.table.setVisible(bool(self.records))
        self.detail_card.setVisible(bool(self.records))
        if self.records and self.table.currentRow() < 0:
            self.table.selectRow(0)
        self._update_details()

    def _activity_selection_changed(self, row: int, checked: bool) -> None:
        """Keep checked-row actions and the visible detail panel in sync."""
        if checked and 0 <= row < self.table.rowCount():
            self.table.selectRow(row)
        self._update_details()

    def _counts_text(self, record: dict) -> str:
        counts = record.get("counts", {})
        if not counts:
            return "—"
        return ", ".join(f"{key}: {value}" for key, value in counts.items())

    def _selected_record(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.records):
            return None
        return self.records[row]

    def _checked_records(self) -> list[dict]:
        checked = []
        for row, record in enumerate(self.records):
            check = self.table.cellWidget(row, 0)
            if isinstance(check, QCheckBox) and check.isChecked():
                checked.append(record)
        return checked

    def _primary_action_record(self) -> dict | None:
        checked = self._checked_records()
        if checked:
            return checked[0]
        return self._selected_record()

    def _update_details(self) -> None:
        record = self._selected_record()
        if not record:
            self.detail_title.setText("No activity selected")
            self.detail_text.setText("Select an activity row to see report path, warnings, failures, and record details.")
            return
        self.detail_title.setText(f"{record.get('title', 'Operation')} • {record.get('status', 'unknown')}")
        details = [
            f"Type: {record.get('type', '—')}",
            f"Created: {record.get('created_at', '')[:19] or '—'}",
            f"Counts: {self._counts_text(record)}",
            f"Report: {record.get('report_path') or 'No report attached'}",
            f"Activity file: {self._record_artifact_path(record) or 'Unavailable'}",
            f"Record ID: {record.get('id', '—')}",
        ]
        if record.get("resume_available"):
            details.append("Resume: available")
        if record.get("warnings"):
            details.append("Warnings: " + "; ".join(str(value) for value in record.get("warnings", [])[:3]))
        if record.get("failures"):
            details.append("Failures: " + "; ".join(str(value) for value in record.get("failures", [])[:3]))
        self.detail_text.setText("\n".join(details))

    def _record_artifact_path(self, record: dict) -> str:
        if record.get("report_path"):
            return str(record["report_path"])
        summary = record.get("summary", {})
        if isinstance(summary, dict) and summary.get("manifest_path"):
            return str(summary["manifest_path"])
        if record.get("path"):
            return str(record["path"])
        return ""

    def _open_activity_path(self, path: str) -> str:
        candidate = Path(path)
        if not candidate.exists():
            raise FileNotFoundError(candidate)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(candidate)))
        return str(candidate)

    def open_selected_report(self) -> None:
        record = self._primary_action_record()
        if not record:
            self.banner.set_message("Select or check an activity row first.", "warning")
            self.banner.show()
            return
        artifact_path = self._record_artifact_path(record)
        if not artifact_path:
            self.banner.set_message("This activity row does not have a local file to open.", "warning")
            self.banner.show()
            return
        try:
            opened = (
                self.reports.open_report_location(record["report_path"])
                if record.get("report_path")
                else self._open_activity_path(artifact_path)
            )
        except Exception as exc:
            self.banner.set_message(f"Could not open selected activity file: {exc}", "danger")
            self.banner.show()
            return
        self.banner.set_message(f"Opened selected activity file: {opened}", "success")
        self.banner.show()

    def open_reports_folder(self) -> None:
        try:
            opened = self.reports.open_reports_folder()
        except Exception as exc:
            self.banner.set_message(f"Could not open reports folder: {exc}", "danger")
            self.banner.show()
            return
        self.banner.set_message(f"Opened reports folder: {opened}", "success")
        self.banner.show()

    def export_selected_report(self) -> None:
        record = self._primary_action_record()
        if not record or not record.get("report_path"):
            self.banner.set_message("Select or check an activity row with a report first.", "warning")
            self.banner.show()
            return
        destination = QFileDialog.getSaveFileName(self, "Export report", Path(record["report_path"]).name, "JSON (*.json)")[0]
        if not destination:
            return
        try:
            exported = self.reports.export_report(
                record["report_path"],
                destination,
                dry_run=self.dry_run_reports.isChecked(),
            )
            prefix = "Would export report to" if self.dry_run_reports.isChecked() else "Report exported to"
            self.banner.set_message(f"{prefix} {exported}.", "success")
        except Exception as exc:
            self.banner.set_message(f"Could not export report: {exc}", "danger")
        self.banner.show()

    def export_activity_csv(self) -> None:
        records = self._checked_records() or self.records
        if not records:
            self.banner.set_message("There is no visible activity to export.", "warning")
            self.banner.show()
            return
        destination = QFileDialog.getSaveFileName(
            self,
            "Export activity CSV",
            "duplicate-transfer-manager-activity.csv",
            "CSV (*.csv)",
        )[0]
        if not destination:
            return
        try:
            exported = self.operations.export_records_csv(
                records,
                destination,
                dry_run=self.dry_run_reports.isChecked(),
            )
            prefix = "Would export" if self.dry_run_reports.isChecked() else "Exported"
            self.banner.set_message(f"{prefix} {len(records)} activity row(s) to {exported}.", "success")
        except Exception as exc:
            self.banner.set_message(f"Could not export activity CSV: {exc}", "danger")
        self.banner.show()

    def resume_selected_import(self) -> None:
        record = self._primary_action_record()
        setup = record.get("summary", {}).get("resume_setup") if record else None
        if not record or record.get("type") != "import" or not record.get("resume_available") or not isinstance(setup, dict):
            self.banner.set_message("Select a cancelled or failed import with resumable setup first.", "warning")
            self.banner.show()
            return
        self.resume_requested.emit(setup)
        self.banner.set_message("Import setup restored. Review it before resuming.", "success")
        self.banner.show()

    def remove_selected_report(self) -> None:
        records = self._checked_records() or ([self._selected_record()] if self._selected_record() else [])
        if not records:
            self.banner.set_message("Select or check an activity row first.", "warning")
            self.banner.show()
            return
        removable = [
            record
            for record in records
            if record.get("report_path") or (record.get("id") and record.get("path"))
        ]
        if not removable:
            self.banner.set_message("The selected activity row(s) do not have removable local records.", "warning")
            self.banner.show()
            return
        if not self.dry_run_reports.isChecked():
            report_count = sum(1 for record in removable if record.get("report_path"))
            record_count = len(removable) - report_count
            response = QMessageBox.question(
                self,
                "Remove activity item",
                (
                    f"Remove {len(removable)} selected activity item(s) from local app storage?\n\n"
                    f"Reports: {report_count}. Activity records: {record_count}.\n"
                    "This does not remove imported, scanned, or quarantined files."
                ),
            )
            if response != QMessageBox.StandardButton.Yes:
                return
        try:
            report_count = 0
            record_count = 0
            for record in removable:
                if record.get("report_path"):
                    self.reports.remove_report(record["report_path"], dry_run=self.dry_run_reports.isChecked())
                    report_count += 1
                elif not self.dry_run_reports.isChecked():
                    self.operations.remove_record(str(record["id"]))
                    record_count += 1
                else:
                    record_count += 1
            action = "Would remove" if self.dry_run_reports.isChecked() else "Removed"
            message = f"{action} {report_count} report(s) and {record_count} activity record(s) from local storage."
            self.banner.set_message(message, "success")
            self.refresh()
        except Exception as exc:
            self.banner.set_message(f"Could not remove selected activity item: {exc}", "danger")
        self.banner.show()


class QuarantinePage(BasePage):
    navigate_requested = Signal(str)

    def __init__(
        self,
        service: DuplicateQuarantineService | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "Quarantine",
            "Recover duplicate files moved aside after a reviewed cleanup operation.",
            parent,
        )
        self.service = service or DuplicateQuarantineService()
        self.records = []
        self.visible_records = []
        self.summary_grid = ResponsiveGrid(
            [MetricCard("0", "Files in quarantine"), MetricCard("0 B", "Recoverable space"), MetricCard("—", "Operations")],
            min_column_width=260,
            max_columns=3,
        )
        self.content.addWidget(self.summary_grid)
        self.content.addWidget(
            InlineMessage(
                "Duplicate & Transfer Manager never permanently deletes files in v1.",
                "success",
            )
        )
        toolbar_card = Card(subtle=True)
        toolbar = QVBoxLayout(toolbar_card)
        toolbar.setContentsMargins(Spacing.LG, Spacing.MD, Spacing.LG, Spacing.MD)
        toolbar.setSpacing(Spacing.SM)
        filter_row = QHBoxLayout()
        filter_row.setSpacing(Spacing.MD)
        action_row = QHBoxLayout()
        action_row.setSpacing(Spacing.MD)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search quarantine")
        self.search.setAccessibleName("Search quarantine")
        self.search.setMinimumWidth(260)
        self.search.textChanged.connect(self.refresh)
        self.filter_choice = QComboBox()
        self.filter_choice.addItems(["Active files", "Local files", "Android copies", "Restored files", "All records"])
        self.filter_choice.setMinimumWidth(150)
        self.filter_choice.currentIndexChanged.connect(self.refresh)
        self.conflict = QComboBox()
        self.conflict.addItem("Rename restored file if a path exists", "rename")
        self.conflict.addItem("Skip if the original path exists", "skip")
        self.conflict.addItem("Replace file at original path", "replace")
        self.conflict.setAccessibleName("Restore conflict policy")
        self.conflict.setMinimumWidth(260)
        self.dry_run_restore = QCheckBox("Dry run restore")
        self.dry_run_restore.setToolTip("Preview restore destinations without moving quarantined files.")
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        open_copy = QPushButton("Open quarantined copy")
        open_copy.clicked.connect(self.open_selected_copy)
        open_original = QPushButton("Open original folder")
        open_original.clicked.connect(self.open_selected_original)
        restore_selected = PrimaryButton("Restore quarantine")
        restore_selected.clicked.connect(self.restore_selected)
        restore_operation = QPushButton("Restore selected operation")
        restore_operation.clicked.connect(self.restore_selected_operation)
        filter_row.addWidget(QLabel("Find"))
        filter_row.addWidget(self.search, 1)
        filter_row.addWidget(QLabel("Show"))
        filter_row.addWidget(self.filter_choice)
        filter_row.addWidget(QLabel("Restore mode"))
        filter_row.addWidget(self.conflict)
        filter_row.addWidget(self.dry_run_restore)
        action_row.addStretch()
        action_row.addWidget(refresh)
        action_row.addWidget(open_copy)
        action_row.addWidget(open_original)
        action_row.addWidget(restore_selected)
        action_row.addWidget(restore_operation)
        toolbar.addLayout(filter_row)
        toolbar.addLayout(action_row)
        self.content.addWidget(toolbar_card)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Restore", "Operation", "Original path", "Quarantined copy", "Size", "Device", "Status"]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(0, 72)
        self.table.setMinimumHeight(340)
        self.table.currentCellChanged.connect(lambda *_args: self._update_preview())
        self.content.addWidget(self.table)

        self.preview_card = Card(subtle=True)
        self.preview_card.setMaximumHeight(180)
        preview_layout = QHBoxLayout(self.preview_card)
        preview_layout.setContentsMargins(Spacing.LG, Spacing.MD, Spacing.LG, Spacing.MD)
        preview_layout.setSpacing(Spacing.LG)
        self.preview_image = QLabel("No preview selected")
        self.preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_image.setMinimumSize(160, 96)
        self.preview_image.setMaximumSize(160, 96)
        self.preview_image.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.preview_detail = QLabel("Select a quarantined file to preview media when possible.")
        self.preview_detail.setWordWrap(True)
        self.preview_detail.setProperty("muted", True)
        preview_layout.addWidget(self.preview_image)
        preview_layout.addWidget(self.preview_detail, 1)
        self.content.addWidget(self.preview_card)

        self.empty = EmptyState(
            "Quarantine is empty",
            "Files appear here only after you review duplicates and confirm a quarantine operation.",
            "Find duplicates",
            "quarantine",
        )
        self.empty.action_requested.connect(lambda: self.navigate_requested.emit("duplicates"))
        self.content.addWidget(self.empty)
        self.banner = ToastBanner("Restore results will appear here.", "info")
        self.banner.hide()
        self.content.addWidget(self.banner)
        self.refresh()
        self.finish()

    def refresh(self) -> None:
        self.records = self.service.list_records()
        query = self.search.text().lower().strip()
        selected_filter = self.filter_choice.currentText()
        active = []
        for record in self.records:
            if selected_filter == "Active files" and record.restored_at:
                continue
            if selected_filter == "Local files" and (record.source_is_adb or record.restored_at):
                continue
            if selected_filter == "Android copies" and (not record.source_is_adb or record.restored_at):
                continue
            if selected_filter == "Restored files" and not record.restored_at:
                continue
            haystack = f"{record.operation_id} {record.original_path} {record.stored_path}".lower()
            if query and query not in haystack:
                continue
            active.append(record)
        self.visible_records = active
        self.table.setRowCount(0)
        for row, record in enumerate(active):
            self.table.insertRow(row)
            check = QCheckBox()
            check.setAccessibleName(f"Restore {record.original_path}")
            check.toggled.connect(lambda checked, target_row=row: self._quarantine_selection_changed(target_row, checked))
            self.table.setCellWidget(row, 0, check)
            self.table.setItem(row, 1, QTableWidgetItem(record.operation_id))
            self.table.setItem(row, 2, QTableWidgetItem(record.original_path))
            self.table.setItem(row, 3, QTableWidgetItem(record.stored_path))
            self.table.setItem(row, 4, QTableWidgetItem(format_duplicate_size(record.size)))
            self.table.setItem(row, 5, QTableWidgetItem("Android" if record.source_is_adb else "This PC"))
            status = (
                f"Restored to {record.restored_path}"
                if record.restored_at
                else "Original remains on Android"
                if record.source_is_adb
                else "Ready to restore"
            )
            self.table.setItem(row, 6, QTableWidgetItem(status))
        total = sum(record.size for record in active)
        operations = len({record.operation_id for record in active})
        self.empty.setVisible(not active)
        self.table.setVisible(bool(active))
        self.preview_card.setVisible(bool(active))
        # Rebuild simple summary cards so the page reflects current quarantine state.
        self.summary_grid.widgets[0].layout().itemAt(0).widget().setText(str(len(active)))
        self.summary_grid.widgets[1].layout().itemAt(0).widget().setText(format_duplicate_size(total))
        self.summary_grid.widgets[2].layout().itemAt(0).widget().setText(str(operations) if operations else "—")
        self._update_preview()

    def _quarantine_selection_changed(self, row: int, checked: bool) -> None:
        """Checking a restore row makes its preview the active selection."""
        if checked and 0 <= row < self.table.rowCount():
            self.table.selectRow(row)
        self._update_preview()

    def _checked_records(self):
        selected = []
        for row, record in enumerate(self.visible_records):
            check = self.table.cellWidget(row, 0)
            if isinstance(check, QCheckBox) and check.isChecked():
                selected.append(record)
        return selected

    def _selected_visible_record(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.visible_records):
            return None
        return self.visible_records[row]

    def _update_preview(self) -> None:
        record = self._selected_visible_record()
        if not record:
            self.preview_image.setText("No preview selected")
            self.preview_image.setPixmap(QPixmap())
            self.preview_detail.setText("Select a quarantined file to preview media when possible.")
            return
        copy_path = Path(record.stored_path)
        detail = [
            f"Original: {record.original_path}",
            f"Quarantined: {record.stored_path}",
            f"Size: {format_duplicate_size(record.size)}",
            "Source: Android copy" if record.source_is_adb else "Source: This PC",
        ]
        if not copy_path.exists():
            self.preview_image.setText("Missing file")
            self.preview_image.setPixmap(QPixmap())
            detail.append("The quarantined copy is missing or was moved outside the app.")
        else:
            pixmap = QPixmap(str(copy_path))
            if not pixmap.isNull():
                self.preview_image.setPixmap(
                    pixmap.scaled(220, 140, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                )
                self.preview_image.setText("")
                detail.append("Preview available.")
            else:
                self.preview_image.setPixmap(QPixmap())
                self.preview_image.setText(copy_path.suffix.lower() or "File")
                detail.append("Preview unavailable for this file type; metadata is shown instead.")
        self.preview_detail.setText("\n".join(detail))

    def _open_local_path(self, path: Path, *, folder: bool = False) -> bool:
        target = path.parent if folder else path
        if not target.exists():
            self.banner.set_message(f"Path is missing: {target}", "warning")
            self.banner.show()
            return False
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        return True

    def open_selected_copy(self) -> None:
        record = self._selected_visible_record()
        if not record:
            self.banner.set_message("Select a quarantined file first.", "warning")
            self.banner.show()
            return
        if self._open_local_path(Path(record.stored_path)):
            self.banner.set_message("Opened quarantined copy.", "success")
            self.banner.show()

    def open_selected_original(self) -> None:
        record = self._selected_visible_record()
        if not record:
            self.banner.set_message("Select a quarantined file first.", "warning")
            self.banner.show()
            return
        if record.source_is_adb:
            self.banner.set_message("Android originals stay on the phone; connect the device to view the original path.", "info")
            self.banner.show()
            return
        if self._open_local_path(Path(record.original_path), folder=True):
            self.banner.set_message("Opened original folder.", "success")
            self.banner.show()

    def restore_selected(self) -> None:
        selected = self._checked_records()
        if not selected:
            self.banner.set_message("Choose at least one quarantined file to restore.", "warning")
            self.banner.show()
            return
        restored = []
        skipped = []
        failures = []
        for record in selected:
            result = self.service.restore_record(
                record,
                conflict_policy=self.conflict.currentData(),
                dry_run=self.dry_run_restore.isChecked(),
            )
            restored.extend(result.restored)
            skipped.extend(result.skipped)
            failures.extend(result.failures)
        self._show_restore_result(len(restored), len(skipped), len(failures))
        self.refresh()

    def restore_selected_operation(self) -> None:
        selected = self._checked_records()
        if not selected:
            self.banner.set_message("Choose a file from the operation you want to restore.", "warning")
            self.banner.show()
            return
        operation_id = selected[0].operation_id
        conflict_policy = self.conflict.currentData()
        dry_run = self.dry_run_restore.isChecked()
        self.banner.set_message("Restoring quarantined files…", "info")
        self.banner.show()
        self.run_in_background(
            lambda: self.service.restore_operation(
                operation_id, conflict_policy=conflict_policy, dry_run=dry_run
            ),
            self._restore_finished,
        )

    def _restore_finished(self, result) -> None:
        self._show_restore_result(len(result.restored), len(result.skipped), len(result.failures))
        self.refresh()

    def _show_restore_result(self, restored: int, skipped: int, failures: int) -> None:
        kind = "success" if failures == 0 else "warning"
        self.banner.set_message(
            f"Restore complete: {restored} restored, {skipped} skipped, {failures} failed.",
            kind,
        )
        self.banner.show()


class SettingsPage(BasePage):
    theme_requested = Signal(str)
    preferences_saved = Signal(object)

    def __init__(
        self,
        settings: AppSettings,
        service: SettingsService,
        dashboard_service: DashboardService | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "Settings",
            "Choose how the application looks and how much technical detail it shows.",
            parent,
        )
        self.settings = settings
        self.service = service
        self.dashboard_service = dashboard_service or DashboardService(service.paths)
        self.scheduled_scans = ScheduledScanService()
        self._initial_schedule_frequency = settings.scheduled_scan_frequency
        self.banner = ToastBanner("Settings saved on this PC.", "success")
        self.banner.hide()
        self.content.addWidget(self.banner)

        appearance = Card()
        layout = QVBoxLayout(appearance)
        layout.setContentsMargins(
            Spacing.XL,
            Spacing.XL,
            Spacing.XL,
            Spacing.XL,
        )
        layout.setSpacing(Spacing.MD)
        layout.addWidget(SectionHeader("Appearance"))
        self.theme = QComboBox()
        self.theme.addItem("Use Windows setting", "system")
        self.theme.addItem("Light", "light")
        self.theme.addItem("Dark", "dark")
        index = self.theme.findData(settings.appearance)
        self.theme.setCurrentIndex(max(0, index))
        self.theme.setAccessibleName("Application theme")
        self.theme.currentIndexChanged.connect(
            lambda: self.theme_requested.emit(self.theme.currentData())
        )
        layout.addWidget(QLabel("Theme"))
        layout.addWidget(self.theme)
        self.content.addWidget(appearance)

        experience = Card()
        exp_layout = QVBoxLayout(experience)
        exp_layout.setContentsMargins(
            Spacing.XL,
            Spacing.XL,
            Spacing.XL,
            Spacing.XL,
        )
        exp_layout.setSpacing(Spacing.MD)
        exp_layout.addWidget(
            SectionHeader(
                "Experience",
                "Simple mode keeps technical controls tucked away. Advanced mode reveals them.",
            )
        )
        self.mode = QComboBox()
        self.mode.addItem("Simple — recommended", "simple")
        self.mode.addItem("Advanced", "advanced")
        self.mode.setCurrentIndex(max(0, self.mode.findData(settings.experience_mode)))
        self.mode.setAccessibleName("Experience mode")
        self.mode.currentIndexChanged.connect(self._sync_advanced_visibility)
        exp_layout.addWidget(self.mode)
        self.default_categories = QLineEdit(", ".join(settings.default_file_categories))
        self.default_categories.setAccessibleName("Default file categories")
        self.default_profile = QComboBox()
        for profile in ("Balanced", "Reliable", "Fast"):
            self.default_profile.addItem(profile, profile)
        self.default_profile.setCurrentIndex(max(0, self.default_profile.findData(settings.default_transfer_profile)))
        exp_layout.addWidget(QLabel("Default file categories"))
        exp_layout.addWidget(self.default_categories)
        exp_layout.addWidget(QLabel("Default transfer profile"))
        exp_layout.addWidget(self.default_profile)
        self.favorite_locations = QLineEdit(", ".join(settings.favorite_locations))
        self.favorite_locations.setAccessibleName("Saved local locations")
        self.favorite_locations.setPlaceholderText(r"D:\Photos, E:\Camera Backup")
        exp_layout.addWidget(QLabel("Saved local locations"))
        exp_layout.addWidget(self.favorite_locations)
        favorite_hint = QLabel("Separate folders with commas. They appear as quick choices for new scans and imports.")
        favorite_hint.setProperty("muted", True)
        favorite_hint.setWordWrap(True)
        exp_layout.addWidget(favorite_hint)
        self.diagnostics = QCheckBox("Share sanitized crash diagnostics")
        self.diagnostics.setChecked(settings.diagnostic_consent)
        self.diagnostics.setToolTip(
            "Disabled by default. Filenames, paths, hashes, and device serials are excluded."
        )
        self.updates = QCheckBox("Check for updates automatically")
        self.updates.setChecked(settings.check_updates_automatically)
        exp_layout.addWidget(self.diagnostics)
        exp_layout.addWidget(self.updates)
        exp_layout.addWidget(
            InlineMessage(
                "All scans, hashes, reports, and settings remain local. "
                "Diagnostics are opt-in and sanitized.",
                "info",
            )
        )
        self.content.addWidget(experience)

        self.advanced_settings = Card()
        advanced_layout = QVBoxLayout(self.advanced_settings)
        advanced_layout.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.XL)
        advanced_layout.setSpacing(Spacing.MD)
        advanced_layout.addWidget(
            SectionHeader(
                "Advanced settings",
                "Visible controls for cache management, Android behavior, diagnostics, and updates.",
            )
        )
        self.cache_days = QSpinBox()
        self.cache_days.setRange(1, 3650)
        self.cache_days.setValue(settings.cache_retention_days)
        self.cache_days.setSuffix(" days cache retention")
        self.sorting_retention_days = QSpinBox()
        self.sorting_retention_days.setRange(1, 3650)
        self.sorting_retention_days.setValue(settings.sorting_history_retention_days)
        self.sorting_retention_days.setSuffix(" days sorting history and undo retention")
        clear_cache = QPushButton("Clear hash and thumbnail caches")
        clear_cache.clicked.connect(self._clear_cache)
        self.android_enabled = QCheckBox("Enable Android features")
        self.android_enabled.setChecked(settings.android_enabled)
        self.android_path = QLineEdit(settings.android_default_path)
        self.android_path.setAccessibleName("Default Android media path")
        self.keep_awake = QCheckBox("Keep Android awake during imports")
        self.keep_awake.setChecked(settings.keep_android_awake)
        platform_tools = DiagnosticsService(service.paths).collect(include_devices=False)["android_platform_tools"]
        # Report what is actually bundled. The build cannot pin an exact
        # version, because Google serves only the latest platform-tools, so
        # this shows the version recorded at build time and the required floor.
        bundled = platform_tools.get("version", "Unknown")
        if platform_tools.get("bundled"):
            tools_summary = f"Bundled Android Platform Tools: {bundled}"
        else:
            tools_summary = (
                "Android Platform Tools: using the adb found on PATH "
                f"(minimum {platform_tools.get('minimum_version', 'Unknown')})"
            )
        self.platform_tools = QLabel(f"{tools_summary} ({platform_tools['license']})")
        self.platform_tools.setProperty("muted", True)
        self.update_channel = QComboBox()
        for channel in ("stable", "beta", "development"):
            self.update_channel.addItem(channel.title(), channel)
        self.update_channel.setCurrentIndex(max(0, self.update_channel.findData(settings.update_channel)))
        advanced_layout.addWidget(self.cache_days)
        advanced_layout.addWidget(self.sorting_retention_days)
        advanced_layout.addWidget(clear_cache)
        advanced_layout.addWidget(self.android_enabled)
        advanced_layout.addWidget(QLabel("Default Android path"))
        advanced_layout.addWidget(self.android_path)
        advanced_layout.addWidget(self.keep_awake)
        advanced_layout.addWidget(self.platform_tools)
        advanced_layout.addWidget(QLabel("Update channel"))
        advanced_layout.addWidget(self.update_channel)
        self.scheduled_frequency = QComboBox()
        self.scheduled_frequency.addItem("Off", "off")
        self.scheduled_frequency.addItem("Daily read-only duplicate scan", "daily")
        self.scheduled_frequency.addItem("Weekly read-only duplicate scan", "weekly")
        self.scheduled_frequency.setCurrentIndex(
            max(0, self.scheduled_frequency.findData(settings.scheduled_scan_frequency))
        )
        self.scheduled_path = PathSelector(
            "Scheduled scan folder",
            "Choose a local folder",
            "Scheduled scans are read-only: they never quarantine, move, or delete files.",
        )
        self.scheduled_path.set_path(settings.scheduled_scan_path)
        self.scheduled_path.browse_requested.connect(self._browse_scheduled_path)
        advanced_layout.addWidget(QLabel("Scheduled duplicate scans"))
        advanced_layout.addWidget(self.scheduled_frequency)
        advanced_layout.addWidget(self.scheduled_path)
        self.content.addWidget(self.advanced_settings)

        save_row = QHBoxLayout()
        save_row.addStretch()
        save = PrimaryButton("Save settings")
        save.clicked.connect(self._save)
        save_row.addWidget(save)
        self.content.addLayout(save_row)
        self._sync_advanced_visibility()
        self.finish()

    def _save(self) -> None:
        previous_schedule_frequency = self.settings.scheduled_scan_frequency
        previous_schedule_path = self.settings.scheduled_scan_path
        self.settings.appearance = self.theme.currentData()
        self.settings.experience_mode = self.mode.currentData()
        self.settings.default_file_categories = [
            value.strip()
            for value in self.default_categories.text().split(",")
            if value.strip()
        ]
        self.settings.default_transfer_profile = self.default_profile.currentData()
        self.settings.favorite_locations = list(
            dict.fromkeys(
                value.strip()
                for value in self.favorite_locations.text().split(",")
                if value.strip()
            )
        )
        self.settings.diagnostic_consent = self.diagnostics.isChecked()
        self.settings.check_updates_automatically = self.updates.isChecked()
        self.settings.cache_retention_days = self.cache_days.value()
        self.settings.sorting_history_retention_days = self.sorting_retention_days.value()
        self.settings.android_enabled = self.android_enabled.isChecked()
        self.settings.android_default_path = self.android_path.text().strip() or "/sdcard/DCIM"
        self.settings.keep_android_awake = self.keep_awake.isChecked()
        self.settings.update_channel = self.update_channel.currentData()
        self.settings.scheduled_scan_frequency = self.scheduled_frequency.currentData()
        self.settings.scheduled_scan_path = self.scheduled_path.path()
        if (
            self.settings.scheduled_scan_frequency != "off"
            or self._initial_schedule_frequency != self.settings.scheduled_scan_frequency
        ):
            try:
                self.scheduled_scans.configure(
                    self.settings.scheduled_scan_path,
                    self.settings.scheduled_scan_frequency,
                    data_root=str(self.service.paths.root),
                )
            except Exception as exc:
                self.settings.scheduled_scan_frequency = previous_schedule_frequency
                self.settings.scheduled_scan_path = previous_schedule_path
                self.scheduled_frequency.blockSignals(True)
                self.scheduled_frequency.setCurrentIndex(
                    max(0, self.scheduled_frequency.findData(previous_schedule_frequency))
                )
                self.scheduled_frequency.blockSignals(False)
                self.scheduled_path.set_path(previous_schedule_path)
                self.banner.set_message(f"Scheduled scans were not changed: {exc}", "warning")
                self.banner.show()
                return
        self.service.save(self.settings)
        self._initial_schedule_frequency = self.settings.scheduled_scan_frequency
        removed = self.dashboard_service.prune_cache(self.settings.cache_retention_days)
        FileOrganizerService(self.service.paths).prune_manifests(self.settings.organization_retention_days)
        SortExecutor(self.service.paths).prune_runs(self.settings.sorting_history_retention_days)
        self.preferences_saved.emit(self.settings)
        self.banner.set_message(
            (
                "Settings saved on this PC."
                if not removed
                else f"Settings saved. Cleared {format_duplicate_size(removed)} of expired cache data."
            ),
            "success",
        )
        self.banner.show()

    def _browse_scheduled_path(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose scheduled scan folder")
        if selected:
            self.scheduled_path.set_path(selected)

    def _clear_cache(self) -> None:
        removed = self.dashboard_service.clear_cache()
        self.banner.set_message(f"Cleared {format_duplicate_size(removed)} of cache data.", "success")
        self.banner.show()

    def _sync_advanced_visibility(self) -> None:
        self.advanced_settings.setVisible(self.mode.currentData() == "advanced")


class HelpPage(BasePage):
    navigate_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            "Help",
            "Learn the safe workflow and find answers without digging through technical logs.",
            parent,
        )
        topics = ResponsiveGrid(
            [
                MetricCard("01", "Find duplicates", "Scan first, then review every group before quarantine."),
                MetricCard("02", "Import safely", "Compare against your library and copy only new content."),
                MetricCard("03", "Connect Android", "Authorize USB debugging and keep the phone unlocked."),
                MetricCard("04", "Recover files", "Restore quarantined files to their original location."),
                MetricCard("05", "Sort safely", "Preview rules and local suggestions, approve exact operations, then undo recoverable runs."),
            ],
            min_column_width=280,
            max_columns=2,
        )
        self.content.addWidget(topics)
        support = Card()
        layout = QVBoxLayout(support)
        layout.setContentsMargins(
            Spacing.XL,
            Spacing.XL,
            Spacing.XL,
            Spacing.XL,
        )
        layout.addWidget(SectionHeader("About this build"))
        layout.addWidget(QLabel(f"Duplicate & Transfer Manager {__version__}"))
        detail = QLabel(
            "Phase 7 release-readiness build • Windows 10 and 11 • Local-first processing"
        )
        detail.setProperty("muted", True)
        layout.addWidget(detail)
        self.content.addWidget(support)
        self.finish()


class FirstRunOnboardingDialog(QDialog):
    def __init__(
        self,
        settings: AppSettings,
        settings_service: SettingsService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.settings_service = settings_service
        self.setWindowTitle("Welcome to Duplicate & Transfer Manager")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.XL)
        title = QLabel("Set up safe local file management")
        title.setProperty("role", "title")
        layout.addWidget(title)
        topics = [
            ("Local scans", "Choose folders or drives; scans are read-only until you confirm an action."),
            ("Android authorization", "Enable USB debugging, keep the phone unlocked, and approve the prompt."),
            ("Privacy", "Files, paths, hashes, and media metadata stay on this PC by default."),
            ("Diagnostics", "Crash diagnostics are off unless you opt in, and reports are sanitized."),
            ("Updates", "Automatic update checks follow your selected channel and never change system ADB."),
            ("Sort Files", "Deterministic rules override local suggestions. Every live run is reviewed, verified, journaled, and undoable where possible."),
        ]
        for heading, body in topics:
            label = QLabel(f"{heading}: {body}")
            label.setWordWrap(True)
            layout.addWidget(label)
        self.diagnostics = QCheckBox("Opt in to sanitized crash diagnostics")
        self.diagnostics.setChecked(settings.diagnostic_consent)
        self.updates = QCheckBox("Check for updates automatically")
        self.updates.setChecked(settings.check_updates_automatically)
        layout.addWidget(self.diagnostics)
        layout.addWidget(self.updates)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Finish setup")
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def accept(self) -> None:
        self.settings.diagnostic_consent = self.diagnostics.isChecked()
        self.settings.check_updates_automatically = self.updates.isChecked()
        self.settings.onboarding_completed = True
        self.settings_service.save(self.settings)
        super().accept()
