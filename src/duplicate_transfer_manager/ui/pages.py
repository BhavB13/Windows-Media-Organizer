"""Connected static screens for the Phase 2 application shell."""

from __future__ import annotations

from collections import deque
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QPixmap
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
)

from models import Settings
from transfer_safety import cleanup_partial_files
from utils import DEFAULT_EXCLUDES, DEFAULT_MEDIA_EXTS, HashCache

from ..controllers import DuplicateScanController, TransferController
from ..core import AppSettings
from ..runtime_paths import get_runtime_paths
from ..services import (
    DiagnosticsService,
    DuplicateQuarantineService,
    DuplicateReview,
    DashboardService,
    OperationRecordService,
    ReportService,
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
            Spacing.XXL,
            Spacing.XL,
            Spacing.XXL,
            Spacing.XXL,
        )
        self.content.setSpacing(Spacing.LG)
        self.content.addWidget(PageHeader(title, subtitle))
        viewport_layout.addStretch(1)
        viewport_layout.addWidget(self.canvas, 20)
        viewport_layout.addStretch(1)
        self.setWidget(self.viewport_container)

    def finish(self) -> None:
        self.content.addStretch(1)


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
        hero = Card()
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(
            Spacing.XXL,
            Spacing.XXL,
            Spacing.XXL,
            Spacing.XXL,
        )
        hero_layout.setSpacing(0)
        hero_text = QWidget()
        text = QVBoxLayout(hero_text)
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(Spacing.SM)
        eyebrow = QLabel("SAFE, LOCAL FILE MANAGEMENT")
        eyebrow.setProperty("muted", True)
        heading = QLabel("Bring order to your files\nwithout risking the originals.")
        heading.setProperty("role", "display")
        heading.setWordWrap(True)
        detail = QLabel(
            "Find byte-for-byte duplicates or import only files that are not "
            "already in your library. Processing stays on this PC."
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
        actions.addWidget(find_button)
        actions.addWidget(import_button)
        actions.addStretch()
        text.addWidget(eyebrow)
        text.addWidget(heading)
        text.addWidget(detail)
        text.addSpacing(Spacing.MD)
        text.addLayout(actions)

        safety = Card(subtle=True)
        safety_layout = QVBoxLayout(safety)
        safety_layout.setContentsMargins(
            Spacing.XL,
            Spacing.XL,
            Spacing.XL,
            Spacing.XL,
        )
        safety_icon = QLabel()
        safety_icon.setPixmap(icon("quarantine", "#17803D", 32).pixmap(32, 32))
        safety_title = QLabel("Designed for safe decisions")
        safety_title.setProperty("role", "section")
        safety_title.setWordWrap(True)
        safety_text = QLabel(
            "Transfers are copy-only. Duplicate cleanup uses recoverable "
            "quarantine and always includes a review step."
        )
        safety_text.setProperty("muted", True)
        safety_text.setWordWrap(True)
        safety_layout.addWidget(safety_icon)
        safety_layout.addWidget(safety_title)
        safety_layout.addWidget(safety_text)
        hero_layout.addWidget(
            ResponsiveGrid(
                [hero_text, safety],
                min_column_width=420,
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
            max_columns=4,
        )
        self.content.addWidget(self.metrics)

        self.recent = CompletionSummary("Recent activity")
        self.content.addWidget(self.recent)
        self.interrupted = CompletionSummary("Interrupted or resumable work")
        self.content.addWidget(self.interrupted)
        self.storage = CompletionSummary("Local storage")
        self.content.addWidget(self.storage)
        self.refresh()
        self.finish()

    def refresh(self) -> None:
        summary = self.dashboard_service.summary()
        recent = summary["recent_operations"]
        interrupted = summary["interrupted_transfers"]
        devices = summary.get("connected_devices", [])
        self.metrics.widgets[0].layout().itemAt(0).widget().setText(format_duplicate_size(summary["recoverable_bytes"]))
        self.metrics.widgets[1].layout().itemAt(0).widget().setText(str(len(recent)))
        self.metrics.widgets[2].layout().itemAt(0).widget().setText(str(len(interrupted)))
        self.metrics.widgets[3].layout().itemAt(0).widget().setText(str(len(devices)))
        self.recent.set_metrics(
            [
                (
                    record.get("title", "Operation"),
                    f"{record.get('status', 'unknown')} • {record.get('created_at', '')[:19]}",
                )
                for record in recent[:6]
            ]
            or [("No activity yet", "Run a scan or import to populate the dashboard.")]
        )
        self.interrupted.set_metrics(
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
        self.storage.set_metrics(
            [
                ("Cache", format_duplicate_size(storage["cache_bytes"])),
                ("Reports", format_duplicate_size(storage["reports_bytes"])),
                ("Quarantine", format_duplicate_size(storage["quarantine_bytes"])),
                ("Connected devices", ", ".join(str(device.get("serial", device)) for device in devices[:3]) or "None"),
                ("Data root", summary["runtime_root"]),
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
            StepIndicator(["Source", "Options", "Summary", "Scan", "Review", "Quarantine"])
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
        self.source_picker.selection_changed.connect(self._source_changed)
        self.device_choice = QComboBox()
        self.device_choice.setAccessibleName("Android device")
        self.device_choice.hide()
        source_layout.addWidget(self.source_picker)
        source_layout.addWidget(self.device_choice)
        source_layout.addWidget(self.path)
        self.content.addWidget(source_card)

        options = Card()
        options_layout = QVBoxLayout(options)
        options_layout.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.XL)
        options_layout.setSpacing(Spacing.MD)
        options_layout.addWidget(
            SectionHeader(
                "2. Select file categories",
                "Technical hashing and exclusion controls stay tucked inside Advanced options.",
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
            filters.addWidget(widget)
        filters.addStretch()
        options_layout.addLayout(filters)

        keep_row = QHBoxLayout()
        keep_row.addWidget(QLabel("Default copy to keep:"))
        self.oldest = QRadioButton("Oldest")
        self.newest = QRadioButton("Newest")
        self.oldest.setChecked(True)
        self.oldest.toggled.connect(lambda checked: checked and self._apply_preference("oldest"))
        self.newest.toggled.connect(lambda checked: checked and self._apply_preference("newest"))
        keep_row.addWidget(self.oldest)
        keep_row.addWidget(self.newest)
        keep_row.addStretch()
        options_layout.addLayout(keep_row)

        advanced = DisclosurePanel()
        advanced.body_layout.addWidget(QLabel("Hash algorithm"))
        self.hash_choice = QComboBox()
        self.hash_choice.addItem("SHA-256 — recommended", "sha256")
        self.hash_choice.addItem("MD5 — compatibility", "md5")
        advanced.body_layout.addWidget(self.hash_choice)
        advanced.body_layout.addWidget(QLabel("Hash mode"))
        self.hash_mode = QComboBox()
        self.hash_mode.addItem("Full content — safest", "full")
        self.hash_mode.addItem("Fast — large-file sampling", "fast")
        advanced.body_layout.addWidget(self.hash_mode)
        self.threads = QSpinBox()
        self.threads.setRange(1, 16)
        self.threads.setValue(4)
        self.threads.setPrefix("Hash workers: ")
        advanced.body_layout.addWidget(self.threads)
        self.min_size = QSpinBox()
        self.min_size.setRange(0, 1024 * 1024)
        self.min_size.setSuffix(" KB minimum")
        advanced.body_layout.addWidget(self.min_size)
        self.exclusions = QLineEdit(", ".join(DEFAULT_EXCLUDES))
        self.exclusions.setAccessibleName("Excluded folder names")
        advanced.body_layout.addWidget(QLabel("Excluded folders"))
        advanced.body_layout.addWidget(self.exclusions)
        options_layout.addWidget(advanced)
        self.content.addWidget(options)

        self.summary_card = Card()
        summary_layout = QVBoxLayout(self.summary_card)
        summary_layout.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.XL)
        summary_layout.addWidget(SectionHeader("3. Review scan summary"))
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
                "5. Review duplicate groups",
                "The checked rows will be moved into app-managed quarantine. The selected Keep row stays in place.",
            )
        )
        self.recoverable_label = QLabel("Estimated recoverable space: 0 B")
        self.recoverable_label.setProperty("role", "section")
        results_layout.addWidget(self.recoverable_label)
        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            ["Group", "Keep", "Quarantine", "Preview", "Filename", "Path", "Size", "Date", "Device"]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        results_layout.addWidget(self.table)
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
        self.finish()

    def _browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose scan location")
        if selected:
            self.path.set_path(selected)

    def _source_changed(self, source: str) -> None:
        is_android = source == "android"
        self.device_choice.setVisible(is_android)
        if is_android:
            self.path.entry.setPlaceholderText("/sdcard/DCIM")
            self.path.helper.setText("Authorize USB debugging, then choose or type an Android folder.")
            self._refresh_devices()
        else:
            self.path.entry.setPlaceholderText("Choose a folder or drive")
            self.path.helper.setText("Subfolders are included automatically.")

    def _refresh_devices(self) -> None:
        from adb_bridge import ADBBridge

        self.device_choice.clear()
        devices = ADBBridge.list_devices()
        for device in devices:
            label = f"{device.get('model') or device.get('serial')} — {device.get('status', 'unknown')}"
            self.device_choice.addItem(label, device.get("serial", ""))
        if not devices:
            self.device_choice.addItem("No authorized Android device found", "")

    def _selected_categories(self) -> tuple[bool, list[str]]:
        extensions: list[str] = []
        if self.pictures.isChecked():
            extensions.extend([".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".heic", ".webp"])
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
            "Next step: run a read-only scan. Files cannot be moved until you review results and confirm quarantine."
        )
        self.summary_card.show()
        self.scan_button.setEnabled(True)
        self.banner.set_message("Scan setup reviewed. You can run the read-only duplicate scan now.", "success")
        self.banner.show()

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
        metrics = f"{value}%  •  {processed}/{total or '—'}  •  ETA {int(event.eta_seconds) if event.eta_seconds else '—'}"
        self.progress_panel.update_progress(value, event.message, detail, metrics)

    def _on_scan_completed(self, result) -> None:
        self.review_button.setEnabled(True)
        self.scan_button.setEnabled(True)
        self.progress_panel.hide()
        prefer = "newest" if self.newest.isChecked() else "oldest"
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
            check.toggled.connect(self._refresh_recoverable)
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

    def _apply_preference(self, prefer: str) -> None:
        if not self.review:
            return
        rebuilt = []
        for group in self.review.groups:
            ordered = sorted(group.items, key=lambda item: (item.modified, item.path.lower()))
            keep = ordered[-1] if prefer == "newest" else ordered[0]
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
        response = QMessageBox.question(
            self,
            "Confirm quarantine",
            "Move the checked local duplicates into app-managed quarantine?\n\n"
            "Android files are copied into quarantine; phone originals are left untouched.",
        )
        if response != QMessageBox.StandardButton.Yes:
            return
        result = self.quarantine_service.quarantine(
            self.review,
            selected,
            adb_serial=self.adb_serial,
        )
        self.operation_service.record(
            "duplicate_quarantine",
            "warning" if result.failures else "completed",
            title="Duplicate quarantine",
            counts={"quarantined": result.quarantined_count, "failures": len(result.failures)},
            summary={"manifest_path": result.manifest_path},
            failures=list(result.failures),
        )
        if result.failures:
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
        self.source_picker.select("phone")
        self.source_picker.selection_changed.connect(self._source_changed)
        self.device_choice = QComboBox()
        self.device_choice.setAccessibleName("Android device")
        self.source_path = PathSelector(
            "Import from",
            "/sdcard/DCIM",
            "Source files remain unchanged.",
        )
        self.source_path.browse_requested.connect(
            lambda: self._browse(self.source_path, "Choose import source")
        )
        source_layout.addWidget(self.source_picker)
        source_layout.addWidget(self.device_choice)
        source_layout.addWidget(self.source_path)
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
        self.output_path = PathSelector(
            "Save new files to",
            "Leave blank to save into the existing library",
            "When blank, new files are copied into the existing library while preserving source folders.",
        )
        self.library_path.browse_requested.connect(
            lambda: self._browse(self.library_path, "Choose existing library")
        )
        self.output_path.browse_requested.connect(
            lambda: self._browse(self.output_path, "Choose save location")
        )
        destination_layout.addWidget(self.library_path)
        destination_layout.addWidget(SectionHeader("3. Choose where new files should be saved"))
        destination_layout.addWidget(self.output_path)
        self.same_location_message = InlineMessage(
            "If left blank, the existing library and save location are the same. "
            "Duplicate & Transfer Manager will still compare first, then copy only new files.",
            "info",
        )
        destination_layout.addWidget(self.same_location_message)
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
            option.setChecked(checked)
            self.category_checks[key] = option
            type_row.addWidget(option)
        type_row.addStretch()
        type_layout.addLayout(type_row)

        self.profile = QComboBox()
        for name, values in TRANSFER_PROFILES.items():
            suffix = " — recommended" if name == "Balanced" else ""
            self.profile.addItem(f"{name}{suffix}", name)
        self.profile.currentIndexChanged.connect(self._profile_changed)
        self.profile_description = QLabel(TRANSFER_PROFILES["Balanced"]["description"])
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
        self.worker_count = QSpinBox()
        self.worker_count.setRange(0, 16)
        self.worker_count.setSpecialValueText("Profile default")
        self.retry_count = QSpinBox()
        self.retry_count.setRange(0, 10)
        self.retry_count.setSpecialValueText("Profile default")
        self.conflict = QComboBox()
        self.conflict.addItem("Rename if a filename exists", "rename")
        self.conflict.addItem("Skip existing filename", "skip")
        self.conflict.addItem("Replace existing filename", "replace")
        self.use_cache = QCheckBox("Use existing library cache")
        self.use_cache.setChecked(True)
        self.update_cache = QCheckBox("Update caches after successful copy")
        self.update_cache.setChecked(True)
        self.use_adb_cache = QCheckBox("Use Android hash cache")
        self.use_adb_cache.setChecked(True)
        self.keep_awake = QCheckBox("Keep Android awake during transfer")
        self.keep_awake.setChecked(True)
        self.reconnect_timeout = QSpinBox()
        self.reconnect_timeout.setRange(30, 3600)
        self.reconnect_timeout.setValue(300)
        self.reconnect_timeout.setSuffix(" sec reconnect timeout")
        self.stall_timeout = QSpinBox()
        self.stall_timeout.setRange(30, 1800)
        self.stall_timeout.setValue(180)
        self.stall_timeout.setSuffix(" sec stall timeout")
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
        for widget in (self.use_cache, self.update_cache, self.use_adb_cache, self.keep_awake):
            advanced.body_layout.addWidget(widget)
        advanced.body_layout.addWidget(self.reconnect_timeout)
        advanced.body_layout.addWidget(self.stall_timeout)
        advanced.body_layout.addWidget(cleanup_partials)
        type_layout.addWidget(advanced)
        self.content.addWidget(file_types)

        self.content.addWidget(
            InlineMessage(
                "Imports are copy-only and structure-preserving by default. Source files are not modified.",
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
        self._source_changed("phone")
        self.finish()

    def _browse(self, selector: PathSelector, title: str) -> None:
        selected = QFileDialog.getExistingDirectory(self, title)
        if selected:
            selector.set_path(selected)

    def _source_changed(self, source: str) -> None:
        is_phone = source == "phone"
        self.device_choice.setVisible(is_phone)
        self.use_adb_cache.setVisible(is_phone)
        self.keep_awake.setVisible(is_phone)
        if is_phone:
            self.source_path.entry.setPlaceholderText("/sdcard/DCIM")
            self._refresh_devices()
        else:
            self.source_path.entry.setPlaceholderText("Choose a folder or drive")

    def _refresh_devices(self) -> None:
        from adb_bridge import ADBBridge

        self.device_choice.clear()
        devices = ADBBridge.list_devices()
        for device in devices:
            label = f"{device.get('model') or device.get('serial')} — {device.get('status', 'unknown')}"
            self.device_choice.addItem(label, device.get("serial", ""))
        if not devices:
            self.device_choice.addItem("No authorized Android device found", "")

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
        )

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
                ("Profile", f"{review.profile}: {review.profile_description}"),
                ("Advanced", review.advanced_summary),
                ("Location note", same_text),
            ]
        )
        self.review_card.show()
        self.run_button.setEnabled(True)
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
        removed = cleanup_partial_files(root)
        self.banner.set_message(f"Removed {len(removed)} partial transfer file(s).", "success")
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
        metrics = f"{value}%  •  {processed}/{total or '—'}  •  ETA {int(event.eta_seconds) if event.eta_seconds else '—'}"
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
            summary={"resume": "Completed files remain recorded for resume."},
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
            failures=[getattr(error, "message", str(error))],
            resume_available=True,
        )
        self.banner.set_message(getattr(error, "message", str(error)), "danger")
        self.banner.show()


class ActivityPage(BasePage):
    navigate_requested = Signal(str)

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
        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search activity")
        self.search.setAccessibleName("Search activity")
        self.search.textChanged.connect(self.refresh)
        self.filter_choice = QComboBox()
        self.filter_choice.addItems(["All operations", "Duplicate scans", "Imports", "Warnings", "Reports"])
        self.filter_choice.currentIndexChanged.connect(self.refresh)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        open_report = QPushButton("Open report")
        open_report.clicked.connect(self.open_selected_report)
        export_report = QPushButton("Export report")
        export_report.clicked.connect(self.export_selected_report)
        remove_report = QPushButton("Remove report")
        remove_report.clicked.connect(self.remove_selected_report)
        toolbar.addWidget(self.search, 1)
        toolbar.addWidget(self.filter_choice)
        toolbar.addWidget(refresh)
        toolbar.addWidget(open_report)
        toolbar.addWidget(export_report)
        toolbar.addWidget(remove_report)
        self.content.addLayout(toolbar)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Type", "Status", "Created", "Title", "Counts", "Report", "Record ID"]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.content.addWidget(self.table)
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
        for record in self.operations.list_records():
            if selected_filter == "Duplicate scans" and record.get("type") != "duplicate_scan":
                continue
            if selected_filter == "Imports" and record.get("type") != "import":
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
            counts = record.get("counts", {})
            count_text = ", ".join(f"{key}: {value}" for key, value in counts.items()) or "—"
            for column, value in enumerate(
                [
                    record.get("type", ""),
                    record.get("status", ""),
                    record.get("created_at", "")[:19],
                    record.get("title", ""),
                    count_text,
                    record.get("report_path", ""),
                    record.get("id", ""),
                ]
            ):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        self.empty.setVisible(not self.records)
        self.table.setVisible(bool(self.records))

    def _selected_record(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self.records):
            return None
        return self.records[row]

    def open_selected_report(self) -> None:
        record = self._selected_record()
        if not record or not record.get("report_path"):
            self.banner.set_message("Select an activity row with a report first.", "warning")
            self.banner.show()
            return
        try:
            payload = self.reports.load_report(record["report_path"])
        except Exception as exc:
            self.banner.set_message(f"Could not open report: {exc}", "danger")
            self.banner.show()
            return
        self.banner.set_message(f"Report opened locally with {len(payload)} top-level field(s).", "success")
        self.banner.show()

    def export_selected_report(self) -> None:
        record = self._selected_record()
        if not record or not record.get("report_path"):
            self.banner.set_message("Select an activity row with a report first.", "warning")
            self.banner.show()
            return
        destination = QFileDialog.getSaveFileName(self, "Export report", Path(record["report_path"]).name, "JSON (*.json)")[0]
        if not destination:
            return
        try:
            exported = self.reports.export_report(record["report_path"], destination)
            self.banner.set_message(f"Report exported to {exported}.", "success")
        except Exception as exc:
            self.banner.set_message(f"Could not export report: {exc}", "danger")
        self.banner.show()

    def remove_selected_report(self) -> None:
        record = self._selected_record()
        if not record or not record.get("report_path"):
            self.banner.set_message("Select an activity row with a report first.", "warning")
            self.banner.show()
            return
        try:
            self.reports.remove_report(record["report_path"])
            self.banner.set_message("Report removed from local storage.", "success")
            self.refresh()
        except Exception as exc:
            self.banner.set_message(f"Could not remove report: {exc}", "danger")
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
            [MetricCard("0", "Files in quarantine"), MetricCard("0 B", "Recoverable space"), MetricCard("—", "Operations")]
        )
        self.content.addWidget(self.summary_grid)
        self.content.addWidget(
            InlineMessage(
                "Duplicate & Transfer Manager never permanently deletes files in v1.",
                "success",
            )
        )
        tools = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search quarantine")
        self.search.setAccessibleName("Search quarantine")
        self.search.textChanged.connect(self.refresh)
        self.filter_choice = QComboBox()
        self.filter_choice.addItems(["Active files", "Local files", "Android copies", "Restored files", "All records"])
        self.filter_choice.currentIndexChanged.connect(self.refresh)
        self.conflict = QComboBox()
        self.conflict.addItem("Rename restored file if a path exists", "rename")
        self.conflict.addItem("Skip if the original path exists", "skip")
        self.conflict.addItem("Replace file at original path", "replace")
        self.conflict.setAccessibleName("Restore conflict policy")
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        restore_selected = PrimaryButton("Restore selected")
        restore_selected.clicked.connect(self.restore_selected)
        restore_operation = QPushButton("Restore selected operation")
        restore_operation.clicked.connect(self.restore_selected_operation)
        tools.addWidget(self.search, 1)
        tools.addWidget(self.filter_choice)
        tools.addWidget(self.conflict)
        tools.addStretch()
        tools.addWidget(refresh)
        tools.addWidget(restore_selected)
        tools.addWidget(restore_operation)
        self.content.addLayout(tools)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Restore", "Operation", "Original path", "Quarantined copy", "Size", "Device", "Status"]
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.content.addWidget(self.table)

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
        # Rebuild simple summary cards so the page reflects current quarantine state.
        self.summary_grid.widgets[0].layout().itemAt(0).widget().setText(str(len(active)))
        self.summary_grid.widgets[1].layout().itemAt(0).widget().setText(format_duplicate_size(total))
        self.summary_grid.widgets[2].layout().itemAt(0).widget().setText(str(operations) if operations else "—")

    def _checked_records(self):
        selected = []
        for row, record in enumerate(self.visible_records):
            check = self.table.cellWidget(row, 0)
            if isinstance(check, QCheckBox) and check.isChecked():
                selected.append(record)
        return selected

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
        result = self.service.restore_operation(
            operation_id,
            conflict_policy=self.conflict.currentData(),
        )
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
        clear_cache = QPushButton("Clear hash and thumbnail caches")
        clear_cache.clicked.connect(self._clear_cache)
        self.android_enabled = QCheckBox("Enable Android features")
        self.android_enabled.setChecked(settings.android_enabled)
        self.android_path = QLineEdit(settings.android_default_path)
        self.android_path.setAccessibleName("Default Android media path")
        self.keep_awake = QCheckBox("Keep Android awake during imports")
        self.keep_awake.setChecked(settings.keep_android_awake)
        platform_tools = DiagnosticsService(service.paths).collect(include_devices=False)["android_platform_tools"]
        self.platform_tools = QLabel(
            f"Pinned Android Platform Tools: {platform_tools['pinned_version']} "
            f"({platform_tools['license']})"
        )
        self.platform_tools.setProperty("muted", True)
        self.update_channel = QComboBox()
        for channel in ("stable", "beta", "development"):
            self.update_channel.addItem(channel.title(), channel)
        self.update_channel.setCurrentIndex(max(0, self.update_channel.findData(settings.update_channel)))
        advanced_layout.addWidget(self.cache_days)
        advanced_layout.addWidget(clear_cache)
        advanced_layout.addWidget(self.android_enabled)
        advanced_layout.addWidget(QLabel("Default Android path"))
        advanced_layout.addWidget(self.android_path)
        advanced_layout.addWidget(self.keep_awake)
        advanced_layout.addWidget(self.platform_tools)
        advanced_layout.addWidget(QLabel("Update channel"))
        advanced_layout.addWidget(self.update_channel)
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
        self.settings.appearance = self.theme.currentData()
        self.settings.experience_mode = self.mode.currentData()
        self.settings.default_file_categories = [
            value.strip()
            for value in self.default_categories.text().split(",")
            if value.strip()
        ]
        self.settings.default_transfer_profile = self.default_profile.currentData()
        self.settings.diagnostic_consent = self.diagnostics.isChecked()
        self.settings.check_updates_automatically = self.updates.isChecked()
        self.settings.cache_retention_days = self.cache_days.value()
        self.settings.android_enabled = self.android_enabled.isChecked()
        self.settings.android_default_path = self.android_path.text().strip() or "/sdcard/DCIM"
        self.settings.keep_android_awake = self.keep_awake.isChecked()
        self.settings.update_channel = self.update_channel.currentData()
        self.service.save(self.settings)
        self.banner.show()

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
            "Phase 2 design-system preview • Windows 10 and 11 • Local-first processing"
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
