"""Modern profile-driven Sort Files workspace."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, Signal, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..controllers import SortController
from ..core import AppSettings
from ..runtime_paths import RuntimePaths, get_runtime_paths
from ..services import OperationRecordService, SettingsService
from ..sorting import (
    Association,
    ConflictPolicy,
    ConditionField,
    ConditionOperator,
    HybridSortService,
    MatchMode,
    MonitoredFolder,
    MLSuggestion,
    SortAction,
    SortCondition,
    SortExecutor,
    SortMonitorService,
    SortScheduleService,
    SortSetup,
    SortPlan,
    SortWorkflowSession,
    SortingProfile,
    SortingMigrationService,
    SortingProfileStore,
    DEFAULT_SELECTED_CATEGORIES,
    DEFAULT_SORT_CATEGORIES,
    QuickSortOptions,
    build_quick_profile,
    parse_extensions,
)
from .theme import Spacing
from .widgets import Card, DisclosurePanel, MetricCard, PageHeader, PrimaryButton, ProgressPanel, ResponsiveGrid, SectionHeader, ToastBanner, format_eta


class DropZone(QFrame):
    paths_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setProperty("subtleCard", True)
        self.setMinimumHeight(110)
        layout = QVBoxLayout(self)
        self.label = QLabel("Drop files or folders here\n—or use Add files / Add folder")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setProperty("muted", True)
        self.label.setAccessibleName("Sort source drop area")
        layout.addWidget(self.label)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.paths_dropped.emit(paths)
            event.acceptProposedAction()


class ConditionDialog(QDialog):
    def __init__(self, condition: SortCondition | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Association condition")
        layout = QFormLayout(self)
        self.field = QComboBox()
        for value in ConditionField:
            self.field.addItem(value.value.replace("_", " ").title(), value)
        self.operator = QComboBox()
        for value in ConditionOperator:
            self.operator.addItem(value.value.replace("_", " ").title(), value)
        self.value = QLineEdit()
        self.exclude = QCheckBox("Exclude files matching this condition")
        self.case_sensitive = QCheckBox("Case-sensitive text match")
        layout.addRow("Field", self.field)
        layout.addRow("Match", self.operator)
        layout.addRow("Value", self.value)
        layout.addRow("", self.exclude)
        layout.addRow("", self.case_sensitive)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        if condition:
            self.field.setCurrentIndex(self.field.findData(condition.field))
            self.operator.setCurrentIndex(self.operator.findData(condition.operator))
            raw = condition.value
            self.value.setText(", ".join(str(value) for value in raw) if isinstance(raw, (list, tuple)) else str(raw))
            self.exclude.setChecked(condition.exclude)
            self.case_sensitive.setChecked(condition.case_sensitive)

    def condition(self) -> SortCondition:
        raw: object = self.value.text().strip()
        operator = self.operator.currentData()
        if operator == ConditionOperator.IN:
            raw = [value.strip() for value in str(raw).split(",") if value.strip()]
        elif operator == ConditionOperator.BETWEEN:
            values = [value.strip() for value in str(raw).split(",", 1)]
            raw = values if len(values) == 2 else raw
        return SortCondition(self.field.currentData(), operator, raw, self.exclude.isChecked(), self.case_sensitive.isChecked())


class AssociationDialog(QDialog):
    def __init__(self, association: Association | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit association" if association else "New association")
        self.resize(760, 560)
        self._id = association.id if association else ""
        outer = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(association.name if association else "")
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(association.enabled if association else True)
        self.priority = QSpinBox()
        self.priority.setRange(-10000, 10000)
        self.priority.setValue(association.priority if association else 100)
        self.match_mode = QComboBox()
        self.match_mode.addItem("All include conditions", MatchMode.ALL)
        self.match_mode.addItem("Any include condition", MatchMode.ANY)
        self.action = QComboBox()
        for value in SortAction:
            self.action.addItem(value.value.replace("_", " ").title(), value)
        self.destination = QLineEdit(association.destination if association else "")
        self.destination.setPlaceholderText(r"D:\Sorted\{media_type}\{year}")
        self.rename_template = QLineEdit(association.rename_template if association else "{stem}{suffix}")
        self.conflict = QComboBox()
        for value in ConflictPolicy:
            self.conflict.addItem(value.value.replace("_", " ").title(), value)
        form.addRow("Name", self.name)
        form.addRow("", self.enabled)
        form.addRow("Priority (higher wins)", self.priority)
        form.addRow("Condition logic", self.match_mode)
        form.addRow("Action", self.action)
        form.addRow("Destination", self.destination)
        form.addRow("Rename template", self.rename_template)
        form.addRow("Conflict policy", self.conflict)
        outer.addLayout(form)
        if association:
            self.match_mode.setCurrentIndex(self.match_mode.findData(association.match_mode))
            self.action.setCurrentIndex(self.action.findData(association.action))
            self.conflict.setCurrentIndex(self.conflict.findData(association.conflict_policy))
        outer.addWidget(SectionHeader("Conditions and exclusions", "Include conditions use the logic above. Any matching exclusion rejects the association."))
        self.conditions = list(association.conditions if association else ())
        self.condition_table = QTableWidget(0, 4)
        self.condition_table.setHorizontalHeaderLabels(["Type", "Field", "Operator", "Value"])
        self.condition_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.condition_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        outer.addWidget(self.condition_table, 1)
        condition_actions = QHBoxLayout()
        add = QPushButton("Add condition")
        edit = QPushButton("Edit selected")
        remove = QPushButton("Remove selected")
        add.clicked.connect(self._add_condition)
        edit.clicked.connect(self._edit_condition)
        remove.clicked.connect(self._remove_condition)
        condition_actions.addWidget(add)
        condition_actions.addWidget(edit)
        condition_actions.addWidget(remove)
        condition_actions.addStretch()
        outer.addLayout(condition_actions)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._validate_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self._render_conditions()

    def _add_condition(self) -> None:
        dialog = ConditionDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.conditions.append(dialog.condition())
            self._render_conditions()

    def _edit_condition(self) -> None:
        row = self.condition_table.currentRow()
        if row < 0:
            return
        dialog = ConditionDialog(self.conditions[row], self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.conditions[row] = dialog.condition()
            self._render_conditions()

    def _remove_condition(self) -> None:
        row = self.condition_table.currentRow()
        if row >= 0:
            self.conditions.pop(row)
            self._render_conditions()

    def _render_conditions(self) -> None:
        self.condition_table.setRowCount(len(self.conditions))
        for row, condition in enumerate(self.conditions):
            values = ["Exclude" if condition.exclude else "Include", condition.field.value, condition.operator.value, str(condition.value)]
            for column, value in enumerate(values):
                self.condition_table.setItem(row, column, QTableWidgetItem(value))

    def _validate_accept(self) -> None:
        if not self.name.text().strip():
            QMessageBox.warning(self, "Association needs a name", "Give this association a clear name.")
            return
        if self.action.currentData() in {SortAction.MOVE, SortAction.COPY} and not self.destination.text().strip():
            QMessageBox.warning(self, "Destination required", "Move and Copy associations need a destination folder.")
            return
        self.accept()

    def association(self) -> Association:
        values = dict(
            name=self.name.text().strip(), enabled=self.enabled.isChecked(), priority=self.priority.value(),
            match_mode=self.match_mode.currentData(), conditions=tuple(self.conditions), action=self.action.currentData(),
            destination=self.destination.text().strip(), rename_template=self.rename_template.text().strip() or "{stem}{suffix}",
            conflict_policy=self.conflict.currentData(),
        )
        return Association(id=self._id, **values) if self._id else Association(**values)


class ProfileDialog(QDialog):
    def __init__(self, profile: SortingProfile | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Profile settings")
        self._profile = profile
        layout = QFormLayout(self)
        self.name = QLineEdit(profile.name if profile else "")
        self.enabled = QCheckBox("Profile enabled")
        self.enabled.setChecked(profile.enabled if profile else True)
        self.ml_enabled = QCheckBox("Use local ML suggestions when no decisive rule matches")
        self.ml_enabled.setChecked(profile.ml_enabled if profile else True)
        self.high = QDoubleSpinBox()
        self.high.setRange(0.50, 1.00)
        self.high.setSingleStep(0.01)
        self.high.setDecimals(2)
        self.high.setValue(profile.high_confidence if profile else 0.92)
        self.review = QDoubleSpinBox()
        self.review.setRange(0.00, 0.99)
        self.review.setSingleStep(0.01)
        self.review.setDecimals(2)
        self.review.setValue(profile.review_confidence if profile else 0.65)
        self.conflict = QComboBox()
        for value in ConflictPolicy:
            self.conflict.addItem(value.value.replace("_", " ").title(), value)
        self.conflict.setCurrentIndex(self.conflict.findData(profile.default_conflict_policy if profile else ConflictPolicy.REVIEW))
        layout.addRow("Profile name", self.name)
        layout.addRow("", self.enabled)
        layout.addRow("", self.ml_enabled)
        layout.addRow("High-confidence threshold", self.high)
        layout.addRow("Review threshold", self.review)
        layout.addRow("Default conflict policy", self.conflict)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _accept_if_valid(self) -> None:
        if not self.name.text().strip():
            QMessageBox.warning(self, "Profile needs a name", "Give this sorting profile a name.")
            return
        if self.review.value() > self.high.value():
            QMessageBox.warning(self, "Confidence thresholds", "The review threshold cannot exceed the high-confidence threshold.")
            return
        self.accept()

    def profile(self) -> SortingProfile:
        if self._profile:
            return replace(
                self._profile, name=self.name.text().strip(), enabled=self.enabled.isChecked(),
                ml_enabled=self.ml_enabled.isChecked(), high_confidence=self.high.value(),
                review_confidence=self.review.value(), default_conflict_policy=self.conflict.currentData(),
            )
        return SortingProfile(
            self.name.text().strip(), (), enabled=self.enabled.isChecked(), ml_enabled=self.ml_enabled.isChecked(),
            high_confidence=self.high.value(), review_confidence=self.review.value(),
            default_conflict_policy=self.conflict.currentData(),
        )


class MonitorDialog(QDialog):
    def __init__(self, monitor: MonitoredFolder | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Monitored folder")
        self._id = monitor.id if monitor else ""
        layout = QFormLayout(self)
        self.path = QLineEdit(monitor.path if monitor else "")
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path, 1)
        path_row.addWidget(browse)
        self.enabled = QCheckBox("Monitoring enabled")
        self.enabled.setChecked(monitor.enabled if monitor else True)
        self.mode = QComboBox()
        self.mode.addItem("Filesystem changes (local polling)", "filesystem_change")
        self.mode.addItem("Windows scheduled task", "scheduled")
        self.schedule = QComboBox()
        for value in ("off", "hourly", "daily", "weekly"):
            self.schedule.addItem(value.title(), value)
        self.recursive = QCheckBox("Include subfolders")
        self.recursive.setChecked(monitor.recursive if monitor else True)
        self.dry_run = QCheckBox("Dry run only — recommended")
        self.dry_run.setChecked(monitor.dry_run if monitor else True)
        self.live_approved = QCheckBox("I explicitly approve live automated processing for this profile")
        self.live_approved.setChecked(monitor.live_approved if monitor else False)
        if monitor:
            self.mode.setCurrentIndex(self.mode.findData(monitor.scan_mode))
            self.schedule.setCurrentIndex(self.schedule.findData(monitor.schedule))
        layout.addRow("Folder", path_row)
        layout.addRow("", self.enabled)
        layout.addRow("Scan mode", self.mode)
        layout.addRow("Schedule", self.schedule)
        layout.addRow("", self.recursive)
        layout.addRow("", self.dry_run)
        layout.addRow("", self.live_approved)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _browse(self) -> None:
        value = QFileDialog.getExistingDirectory(self, "Choose monitored folder")
        if value:
            self.path.setText(value)

    def _accept_if_valid(self) -> None:
        if not Path(self.path.text().strip()).is_dir():
            QMessageBox.warning(self, "Folder unavailable", "Choose an available local folder.")
            return
        if not self.dry_run.isChecked() and not self.live_approved.isChecked():
            QMessageBox.warning(self, "Live approval required", "Live monitored processing requires the explicit approval checkbox.")
            return
        self.accept()

    def monitor(self) -> MonitoredFolder:
        values = dict(
            path=self.path.text().strip(), enabled=self.enabled.isChecked(), scan_mode=self.mode.currentData(),
            schedule=self.schedule.currentData(), recursive=self.recursive.isChecked(), dry_run=self.dry_run.isChecked(),
            live_approved=self.live_approved.isChecked(),
        )
        return MonitoredFolder(id=self._id, **values) if self._id else MonitoredFolder(**values)


class SortWorkspace(QScrollArea):
    """Dedicated Sort workspace spanning setup, review, processing, and history."""

    SOURCE_STAGE = 0
    RULES_STAGE = 1
    REVIEW_STAGE = 2
    PROCESS_STAGE = 3
    RESULTS_STAGE = 4

    def __init__(
        self,
        paths: RuntimePaths | None = None,
        settings: AppSettings | None = None,
        settings_service: SettingsService | None = None,
        operations: OperationRecordService | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.paths = paths or get_runtime_paths()
        self.settings = settings or AppSettings()
        self.settings_service = settings_service
        self.operations = operations or OperationRecordService(self.paths)
        self.store = SortingProfileStore(self.paths)
        self.service = HybridSortService(self.paths)
        self.controller = SortController(self.service)
        self.monitor = SortMonitorService(self.paths)
        self.scheduler = SortScheduleService()
        self.executor = SortExecutor(self.paths)
        self.session = SortWorkflowSession()
        SortingMigrationService(self.paths).migrate_legacy_runs()
        self.sources: list[str] = []
        self.plan: SortPlan | None = None
        self._planning = False
        self._executing = False
        self._updating_table = False
        self._current_stage = self.SOURCE_STAGE

        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        canvas = QWidget()
        canvas.setObjectName("PageCanvas")
        layout = QVBoxLayout(canvas)
        layout.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.XXL)
        layout.setSpacing(Spacing.LG)
        layout.addWidget(PageHeader("Sort Files", "Move or copy pictures, videos, audio, and documents into clear folders."))
        self.banner = ToastBanner("No file changes occur until a reviewed live plan is confirmed.", "info")
        self.banner.hide()
        layout.addWidget(self.banner)
        self.stack = QStackedWidget()
        self._build_overview()
        self._build_rules()
        self._build_review()
        self._build_processing()
        self._build_history()
        self.source_page = self.stack.widget(self.SOURCE_STAGE)
        self.rules_page = self.stack.widget(self.RULES_STAGE)
        self.review_page = self.stack.widget(self.REVIEW_STAGE)
        self.process_page = self.stack.widget(self.PROCESS_STAGE)
        self.results_page = self.stack.widget(self.RESULTS_STAGE)
        for page in (self.source_page, self.rules_page, self.review_page, self.process_page, self.results_page):
            self.stack.removeWidget(page)
        layout.addWidget(self.source_page)
        self.advanced_panel = DisclosurePanel("Advanced profiles, rules, ML, and automation")
        self.advanced_panel.body_layout.addWidget(self.rules_page)
        self.source_page.show()
        self.rules_page.show()
        layout.addWidget(self.advanced_panel)
        layout.addWidget(self.preview_progress)
        layout.addWidget(self.review_page)
        layout.addWidget(self.process_page)
        layout.addWidget(self.results_page)
        self.review_page.hide()
        self.process_page.hide()
        self.results_page.hide()
        layout.addStretch()
        self.setWidget(canvas)

        self.controller.progress.connect(self._on_progress)
        self.controller.completed.connect(self._on_completed)
        self.controller.cancelled.connect(self._on_cancelled)
        self.controller.failed.connect(self._on_error)
        self.controller.recoverable_error.connect(self._on_error)
        self._ensure_profile()
        self._refresh_profiles()
        self._refresh_history()
        self._show_section(self.SOURCE_STAGE)
        self.change_timer = QTimer(self)
        self.change_timer.setInterval(30_000)
        self.change_timer.timeout.connect(self._poll_change_monitors)
        self.change_timer.start()

    def _show_section(self, index: int) -> None:
        self._current_stage = max(self.SOURCE_STAGE, min(index, self.RESULTS_STAGE))
        if index == self.RULES_STAGE:
            self.advanced_panel.toggle.setChecked(True)
            self.advanced_panel._toggle(True)
            QTimer.singleShot(0, lambda: self._scroll_to_widget(self.advanced_panel))
        elif index == self.REVIEW_STAGE:
            self.review_page.show()
            QTimer.singleShot(0, lambda: self._scroll_to_widget(self.review_page))
        elif index == self.PROCESS_STAGE:
            self.process_page.show()
            QTimer.singleShot(0, lambda: self._scroll_to_widget(self.process_page))
        elif index == self.RESULTS_STAGE:
            self.results_page.show()
            QTimer.singleShot(0, lambda: self._scroll_to_widget(self.results_page))
        else:
            self.horizontalScrollBar().setValue(0)
            self.verticalScrollBar().setValue(0)

    def _scroll_to_widget(self, widget: QWidget) -> None:
        position = widget.mapTo(self.widget(), QPoint(0, 0)).y()
        self.verticalScrollBar().setValue(max(0, position - Spacing.LG))

    def _build_overview(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(Spacing.LG)
        self.stat_runs = MetricCard("0", "Sorting runs", "Local transaction journals")
        self.stat_files = MetricCard("0", "Files processed", "Verified operations")
        self.stat_monitors = MetricCard("0", "Monitored folders", "Enabled in the active profile")
        self.stat_undo = MetricCard("0", "Undo available", "Recoverable completed runs")
        self.stats_grid = ResponsiveGrid(
            (self.stat_runs, self.stat_files, self.stat_monitors, self.stat_undo),
            min_column_width=300, max_columns=3,
        )
        active = Card()
        active_layout = QVBoxLayout(active)
        active_layout.addWidget(SectionHeader("1. Choose files or folders", "Add a folder, individual files, or drag them here. Subfolders are included automatically."))
        self.profile_choice = QComboBox()
        self.profile_choice.setAccessibleName("Active sorting profile")
        self.profile_choice.currentIndexChanged.connect(self._profile_changed)
        self.drop_zone = DropZone()
        self.drop_zone.paths_dropped.connect(self._add_sources)
        active_layout.addWidget(self.drop_zone)
        source_actions = QHBoxLayout()
        add_files = QPushButton("Add files")
        add_folder = QPushButton("Add folder")
        clear = QPushButton("Clear sources")
        add_files.clicked.connect(self._choose_files)
        add_folder.clicked.connect(self._choose_folder)
        clear.clicked.connect(self._clear_sources)
        source_actions.addWidget(add_files)
        source_actions.addWidget(add_folder)
        source_actions.addWidget(clear)
        source_actions.addStretch()
        active_layout.addLayout(source_actions)
        self.source_table = QTableWidget(0, 2)
        self.source_table.setAccessibleName("Selected sorting sources")
        self.source_table.setHorizontalHeaderLabels(["Source", "Type"])
        self.source_table.verticalHeader().hide()
        self.source_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.source_table.setMinimumHeight(120)
        self.source_table.setMaximumHeight(200)
        self.source_table.hide()
        active_layout.addWidget(self.source_table)
        active_layout.addWidget(SectionHeader("2. Choose the destination", "Selected files are placed into category folders inside this location."))
        destination_row = QHBoxLayout()
        self.default_destination = QLineEdit()
        self.default_destination.setAccessibleName("Sorted files destination")
        self.default_destination.setPlaceholderText("Choose the main folder for sorted files")
        self.default_destination.textChanged.connect(lambda _value: self._invalidate_plan("The fallback destination changed."))
        browse_destination = QPushButton("Browse")
        browse_destination.clicked.connect(self._choose_default_destination)
        destination_row.addWidget(self.default_destination, 1)
        destination_row.addWidget(browse_destination)
        active_layout.addLayout(destination_row)
        active_layout.addWidget(SectionHeader("3. Choose file types", "Common media and document formats are already grouped for you."))
        self.category_checks: dict[str, QCheckBox] = {}
        category_widgets: list[QWidget] = []
        for category in DEFAULT_SORT_CATEGORIES:
            option = QCheckBox(category.label)
            option.setChecked(category.key in DEFAULT_SELECTED_CATEGORIES)
            option.setToolTip(f"{category.description}\nDestination: {category.folder}")
            option.setAccessibleName(f"Sort {category.label}")
            option.toggled.connect(lambda _checked: self._simple_setup_changed("The selected file types changed."))
            self.category_checks[category.key] = option
            category_widgets.append(option)
        self.category_grid = ResponsiveGrid(category_widgets, min_column_width=150, max_columns=5)
        active_layout.addWidget(self.category_grid)
        custom_row = QHBoxLayout()
        self.custom_extensions = QLineEdit()
        self.custom_extensions.setAccessibleName("Additional file extensions")
        self.custom_extensions.setPlaceholderText("Optional extensions, for example: .psd, .epub, .pages")
        self.custom_extensions.textChanged.connect(lambda _value: self._simple_setup_changed("The custom extensions changed."))
        self.custom_category = QComboBox()
        self.custom_category.setAccessibleName("Destination category for additional extensions")
        for category in DEFAULT_SORT_CATEGORIES:
            self.custom_category.addItem(f"Put in {category.label}", category.key)
        self.custom_category.setCurrentIndex(self.custom_category.findData("documents"))
        self.custom_category.currentIndexChanged.connect(lambda _index: self._simple_setup_changed("The custom extension destination changed."))
        custom_row.addWidget(self.custom_extensions, 1)
        custom_row.addWidget(self.custom_category)
        active_layout.addLayout(custom_row)
        self.category_summary = QLabel()
        self.category_summary.setProperty("muted", True)
        self.category_summary.setWordWrap(True)
        active_layout.addWidget(self.category_summary)
        options = QVBoxLayout()
        safety_options = QGridLayout()
        self.simple_action = QComboBox()
        self.simple_action.setAccessibleName("Sort action")
        self.simple_action.addItem("Move files", SortAction.MOVE)
        self.simple_action.addItem("Copy files", SortAction.COPY)
        self.simple_action.setToolTip("Move is recommended for organizing. Copy keeps the originals in place.")
        self.simple_action.currentIndexChanged.connect(lambda _index: self._invalidate_plan("The sorting action changed."))
        self.simple_conflict = QComboBox()
        self.simple_conflict.setAccessibleName("Existing filename handling")
        self.simple_conflict.addItem("Rename the new file", ConflictPolicy.RENAME)
        self.simple_conflict.addItem("Skip the new file", ConflictPolicy.SKIP)
        self.simple_conflict.addItem("Review the conflict", ConflictPolicy.REVIEW)
        self.simple_conflict.setToolTip("Controls what happens when the destination already contains the same filename.")
        self.simple_conflict.currentIndexChanged.connect(lambda _index: self._invalidate_plan("The existing-file handling changed."))
        self.dry_run = QCheckBox("Dry run — no file changes")
        self.dry_run.setChecked(False)
        self.dry_run.setToolTip("The Run action writes a report but leaves every source and destination file unchanged.")
        self.dry_run.toggled.connect(lambda _checked: self._invalidate_plan("The run safety mode changed."))
        self.retry_count = QSpinBox()
        self.retry_count.setRange(0, 5)
        self.retry_count.setValue(1)
        self.retry_count.setSuffix(" retries")
        self.continue_button = QPushButton("Review sort setup")
        self.continue_button.setAccessibleName("Review sort setup")
        self.continue_button.clicked.connect(self._preview)
        safety_options.addWidget(QLabel("Action"), 0, 0)
        safety_options.addWidget(QLabel("If a filename exists"), 0, 1)
        safety_options.addWidget(self.simple_action, 1, 0)
        safety_options.addWidget(self.simple_conflict, 1, 1)
        safety_options.addWidget(self.dry_run, 2, 0, 1, 2)
        safety_options.setColumnStretch(0, 1)
        safety_options.setColumnStretch(1, 1)
        options.addLayout(safety_options)
        continue_row = QHBoxLayout()
        continue_row.addStretch()
        continue_row.addWidget(self.continue_button)
        options.addLayout(continue_row)
        active_layout.addLayout(options)
        layout.addWidget(active)
        self.stats_grid.hide()
        self._update_category_summary()
        layout.addStretch()
        self.stack.addWidget(page)

    def _build_rules(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(Spacing.LG)
        setup_card = Card(subtle=True)
        setup_layout = QVBoxLayout(setup_card)
        setup_layout.addWidget(SectionHeader("2. Confirm how files will be sorted", "Rules are applied from highest priority to lowest. Local suggestions are used only when no rule decides the result."))
        self.setup_summary = QLabel("Return to Source to choose files for this run.")
        self.setup_summary.setWordWrap(True)
        self.setup_summary.setAccessibleName("Sorting setup summary")
        setup_layout.addWidget(self.setup_summary)
        layout.addWidget(setup_card)
        setup_card.hide()
        self.use_advanced_profile = QCheckBox("Use a saved advanced profile")
        self.use_advanced_profile.setAccessibleName("Use advanced sorting profile")
        self.use_advanced_profile.setToolTip("When enabled, the saved profile rules below replace the simple media and document categories.")
        self.use_advanced_profile.toggled.connect(self._advanced_mode_changed)
        layout.addWidget(self.use_advanced_profile)
        profile_card = Card()
        profile_layout = QVBoxLayout(profile_card)
        profile_layout.addWidget(SectionHeader("Active profile", "Change profile settings here only when the current profile does not describe the organization you want."))
        profile_layout.addWidget(QLabel("Saved sorting profile"))
        profile_layout.addWidget(self.profile_choice)
        profile_actions: list[QWidget] = []
        for label, slot in (("New profile", self._new_profile), ("Edit settings", self._edit_profile), ("Duplicate", self._duplicate_profile), ("Import", self._import_profile), ("Export", self._export_profile), ("Enable / disable", self._toggle_profile), ("Delete", self._delete_profile)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            profile_actions.append(button)
        self.profile_actions = ResponsiveGrid(profile_actions, min_column_width=150, max_columns=4)
        profile_layout.addWidget(self.profile_actions)
        self.profile_management = DisclosurePanel("Manage profile settings")
        self.profile_management.body_layout.addWidget(profile_card)
        layout.addWidget(self.profile_management)
        rule_card = Card()
        rule_layout = QVBoxLayout(rule_card)
        rule_layout.addWidget(SectionHeader("Rules used for this run", "Each file is evaluated against these associations before any local suggestion is considered."))
        self.rules_status = QLabel()
        self.rules_status.setWordWrap(True)
        self.rules_status.setProperty("muted", True)
        self.rules_status.setAccessibleName("Active sorting rules summary")
        rule_layout.addWidget(self.rules_status)
        self.association_table = QTableWidget(0, 7)
        self.association_table.setAccessibleName("Sorting associations")
        self.association_table.setHorizontalHeaderLabels(["Enabled", "Priority", "Name", "Conditions", "Action", "Destination", "Conflict"])
        self.association_table.verticalHeader().hide()
        self.association_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.association_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.association_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.association_table.setMinimumHeight(220)
        self.association_table.setMaximumHeight(360)
        rule_layout.addWidget(self.association_table)
        rule_actions = QHBoxLayout()
        add = QPushButton("Add association")
        edit = QPushButton("Edit selected")
        remove = QPushButton("Remove selected")
        add.clicked.connect(self._add_association)
        edit.clicked.connect(self._edit_association)
        remove.clicked.connect(self._remove_association)
        rule_actions.addWidget(add)
        rule_actions.addWidget(edit)
        rule_actions.addWidget(remove)
        rule_actions.addStretch()
        rule_layout.addLayout(rule_actions)
        layout.addWidget(rule_card)
        monitor_card = Card()
        monitor_layout = QVBoxLayout(monitor_card)
        monitor_layout.addWidget(SectionHeader("Optional automation", "Monitored folders are separate from this manual run. Configure them only if you want future recurring previews."))
        self.monitor_table = QTableWidget(0, 5)
        self.monitor_table.setAccessibleName("Monitored sorting folders")
        self.monitor_table.setHorizontalHeaderLabels(["Enabled", "Folder", "Mode", "Schedule", "Safety"])
        self.monitor_table.verticalHeader().hide()
        self.monitor_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        monitor_layout.addWidget(self.monitor_table)
        add_monitor = QPushButton("Add monitored folder")
        edit_monitor = QPushButton("Edit selected")
        remove_monitor = QPushButton("Remove selected")
        scan_monitor = QPushButton("Check selected now")
        add_monitor.clicked.connect(self._add_monitor)
        edit_monitor.clicked.connect(self._edit_monitor)
        remove_monitor.clicked.connect(self._remove_monitor)
        scan_monitor.clicked.connect(self._poll_monitor)
        self.monitor_actions = ResponsiveGrid(
            (add_monitor, edit_monitor, remove_monitor, scan_monitor), min_column_width=170, max_columns=4,
        )
        monitor_layout.addWidget(self.monitor_actions)
        self.automation_options = DisclosurePanel("Automation and monitored folders")
        self.automation_options.body_layout.addWidget(monitor_card)
        layout.addWidget(self.automation_options)
        self.preview_progress = ProgressPanel()
        self.preview_progress.cancel_requested.connect(self.controller.cancel)
        self.preview_progress.hide()
        layout.addWidget(self.preview_progress)
        self.preview_button = self.continue_button
        layout.addStretch()
        self.stack.addWidget(page)

    def _build_review(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(Spacing.LG)
        layout.addWidget(SectionHeader("Review proposed changes", "Check the files you want to sort. Unchecked and unsupported files remain where they are."))
        filter_row = QHBoxLayout()
        self.review_filter = QLineEdit()
        self.review_filter.setAccessibleName("Filter sorting preview")
        self.review_filter.setPlaceholderText("Filter source, rule, action, destination, warning, or conflict")
        self.review_filter.textChanged.connect(self._filter_review)
        self.confidence_filter = QComboBox()
        self.confidence_filter.setAccessibleName("Sorting confidence and conflict filter")
        self.confidence_filter.addItems(["All files", "Needs review", "Conflicts", "Warnings"])
        self.confidence_filter.currentIndexChanged.connect(self._filter_review)
        filter_row.addWidget(self.review_filter, 1)
        filter_row.addWidget(self.confidence_filter)
        layout.addLayout(filter_row)
        self.review_summary = QLabel("Complete Source and Rules to build a review.")
        self.review_summary.setWordWrap(True)
        layout.addWidget(self.review_summary)
        self.review_table = QTableWidget(0, 6)
        self.review_table.setAccessibleName("Sorting preview and approval table")
        self.review_table.setHorizontalHeaderLabels(["Sort", "File", "Category", "Action", "Destination", "Note"])
        self.review_table.verticalHeader().hide()
        self.review_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.review_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.review_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.review_table.setMinimumHeight(390)
        self.review_table.itemChanged.connect(self._review_item_changed)
        layout.addWidget(self.review_table)
        back = QPushButton("Change setup")
        back.clicked.connect(lambda: self._show_section(self.SOURCE_STAGE))
        approve_high = QPushButton("Select recommended")
        approve_visible = QPushButton("Select visible")
        clear = QPushButton("Clear selection")
        edit_destination = QPushButton("Edit selected destination")
        approve_high.clicked.connect(self._approve_high)
        approve_visible.clicked.connect(self._approve_visible)
        clear.clicked.connect(self._clear_approvals)
        edit_destination.clicked.connect(self._edit_destination)
        self.run_button = PrimaryButton("Run sort")
        self.run_button.setAccessibleName("Run approved sorting plan")
        self.run_button.clicked.connect(self._run)
        self.run_button.hide()
        self.review_actions = ResponsiveGrid(
            (back, approve_high, approve_visible, clear, edit_destination, self.run_button),
            min_column_width=170, max_columns=6,
        )
        layout.addWidget(self.review_actions)
        layout.addStretch()
        self.stack.addWidget(page)

    def _build_processing(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(Spacing.LG)
        layout.addWidget(SectionHeader("Sorting files", "The operation is journaled and verified. You can pause, skip the current file, or cancel safely."))
        self.processing_summary = QLabel("Processing begins only after the reviewed plan is confirmed.")
        self.processing_summary.setWordWrap(True)
        self.processing_summary.setAccessibleName("Sorting processing summary")
        layout.addWidget(self.processing_summary)
        self.progress = ProgressPanel()
        self.progress.cancel_requested.connect(self.controller.cancel)
        layout.addWidget(self.progress)
        processing_actions = QHBoxLayout()
        self.pause_button = QPushButton("Pause")
        self.resume_button = QPushButton("Resume")
        self.skip_button = QPushButton("Skip current file")
        self.pause_button.clicked.connect(self.controller.pause)
        self.resume_button.clicked.connect(self.controller.resume)
        self.skip_button.clicked.connect(self._skip_current)
        for button in (self.pause_button, self.resume_button, self.skip_button):
            button.hide()
            processing_actions.addWidget(button)
        processing_actions.addStretch()
        layout.addLayout(processing_actions)
        layout.addStretch()
        self.stack.addWidget(page)

    def _build_history(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSpacing(Spacing.LG)
        layout.addWidget(SectionHeader("Sort result and recovery", "Review the completed run, inspect its report, retry failures, or undo recoverable operations."))
        self.result_summary = QLabel("No sorting run has completed in this session.")
        self.result_summary.setWordWrap(True)
        self.result_summary.setAccessibleName("Latest sorting result summary")
        layout.addWidget(self.result_summary)
        self.history_table = QTableWidget(0, 6)
        self.history_table.setAccessibleName("Sorting results and history")
        self.history_table.setHorizontalHeaderLabels(["Run", "Status", "Created", "Completed", "Failed", "Undo"])
        self.history_table.verticalHeader().hide()
        self.history_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.currentCellChanged.connect(lambda *_: self._history_selected())
        layout.addWidget(self.history_table)
        self.history_detail = QLabel("Select a run to inspect its operation log.")
        self.history_detail.setWordWrap(True)
        self.history_detail.setProperty("muted", True)
        layout.addWidget(self.history_detail)
        refresh = QPushButton("Refresh")
        undo = QPushButton("Undo selected run")
        retry = QPushButton("Retry failed / resume")
        open_source = QPushButton("Open source")
        open_destination = QPushButton("Open destination")
        open_report = QPushButton("Open report")
        export_report = QPushButton("Export report")
        start_again = PrimaryButton("Start another sort")
        refresh.clicked.connect(self._refresh_history)
        undo.clicked.connect(self._undo_selected)
        retry.clicked.connect(self._retry_selected)
        open_source.clicked.connect(lambda: self._open_history_path("source"))
        open_destination.clicked.connect(lambda: self._open_history_path("destination"))
        open_report.clicked.connect(self._open_history_report)
        export_report.clicked.connect(self._export_history_report)
        start_again.clicked.connect(self._start_new_run)
        self.history_actions = ResponsiveGrid(
            (refresh, undo, retry, open_source, open_destination, open_report, export_report, start_again),
            min_column_width=160, max_columns=4,
        )
        layout.addWidget(self.history_actions)
        layout.addStretch()
        self.stack.addWidget(page)

    def _ensure_profile(self) -> None:
        profiles = self.store.list()
        if not profiles:
            self.store.save(SortingProfile("Default Sort", (), ml_enabled=True))
        self.store.migrate_organizer_presets(self.settings)

    def _active_profile(self) -> SortingProfile | None:
        return self.store.get(str(self.profile_choice.currentData() or ""))

    def _selected_categories(self) -> tuple[str, ...]:
        return tuple(key for key, option in self.category_checks.items() if option.isChecked())

    def _quick_options(self) -> QuickSortOptions:
        return QuickSortOptions(
            destination_root=self.default_destination.text().strip(),
            selected_categories=self._selected_categories(),
            custom_extensions=parse_extensions(self.custom_extensions.text()),
            custom_category=str(self.custom_category.currentData() or "documents"),
            action=self.simple_action.currentData() or SortAction.MOVE,
            conflict_policy=self.simple_conflict.currentData() or ConflictPolicy.RENAME,
        )

    def _profile_for_run(self) -> SortingProfile:
        if self.use_advanced_profile.isChecked():
            profile = self._active_profile()
            if profile is None or not profile.enabled:
                raise ValueError("Choose an enabled advanced sorting profile.")
            return profile
        return build_quick_profile(self._quick_options())

    def _simple_setup_changed(self, reason: str) -> None:
        self._update_category_summary()
        self._invalidate_plan(reason)

    def _update_category_summary(self) -> None:
        if not hasattr(self, "category_summary"):
            return
        if hasattr(self, "use_advanced_profile") and self.use_advanced_profile.isChecked():
            self.category_summary.setText("Advanced profile mode is enabled. The saved profile rules below replace these simple categories.")
            return
        selected = [category.label for category in DEFAULT_SORT_CATEGORIES if category.key in self._selected_categories()]
        custom = self.custom_extensions.text().strip() if hasattr(self, "custom_extensions") else ""
        summary = ", ".join(selected) if selected else "No default categories"
        if custom:
            summary += f" • Additional: {custom}"
        self.category_summary.setText(f"Selected: {summary}. Other file types will be left in place.")

    def _advanced_mode_changed(self, enabled: bool) -> None:
        for widget in (*self.category_checks.values(), self.custom_extensions, self.custom_category, self.simple_action, self.simple_conflict):
            widget.setEnabled(not enabled)
        self._update_category_summary()
        self._invalidate_plan("The sorting mode changed.")

    def _refresh_profiles(self, select_id: str = "") -> None:
        current = select_id or str(self.profile_choice.currentData() or getattr(self.settings, "active_sorting_profile_id", ""))
        self.profile_choice.blockSignals(True)
        self.profile_choice.clear()
        for profile in self.store.list():
            self.profile_choice.addItem(f"{profile.name}{'' if profile.enabled else ' — disabled'}", profile.id)
        index = self.profile_choice.findData(current)
        self.profile_choice.setCurrentIndex(index if index >= 0 else 0)
        self.profile_choice.blockSignals(False)
        self._profile_changed()

    def _profile_changed(self) -> None:
        profile = self._active_profile()
        if not profile:
            return
        self.settings.active_sorting_profile_id = profile.id
        if self.settings_service:
            self.settings_service.save(self.settings)
        self._render_associations(profile)
        self._render_monitors(profile)
        self._refresh_stats()
        self._update_setup_summary()
        self._invalidate_plan("The active sorting profile changed.")

    def _choose_files(self) -> None:
        values, _ = QFileDialog.getOpenFileNames(self, "Choose files to sort")
        self._add_sources(values)

    def _choose_folder(self) -> None:
        value = QFileDialog.getExistingDirectory(self, "Choose folder to sort")
        self._add_sources([value] if value else [])

    def _choose_default_destination(self) -> None:
        value = QFileDialog.getExistingDirectory(self, "Choose fallback sorting destination")
        if value:
            self.default_destination.setText(value)

    def _add_sources(self, values: list[str]) -> None:
        changed = False
        for value in values:
            path = str(Path(value).expanduser().resolve())
            if Path(path).exists() and path not in self.sources:
                self.sources.append(path)
                changed = True
        if changed:
            self._invalidate_plan("The selected sources changed.")
        self._render_sources()

    def _clear_sources(self) -> None:
        if self.sources:
            self._invalidate_plan("The selected sources were cleared.")
        self.sources.clear()
        self._render_sources()

    def _render_sources(self) -> None:
        self.source_table.setRowCount(len(self.sources))
        self.source_table.setVisible(bool(self.sources))
        for row, value in enumerate(self.sources):
            path = Path(value)
            self.source_table.setItem(row, 0, QTableWidgetItem(value))
            self.source_table.setItem(row, 1, QTableWidgetItem("Folder" if path.is_dir() else "File"))
        self.continue_button.setEnabled(bool(self.sources))
        self._update_setup_summary()

    def _continue_to_rules(self) -> None:
        profile = self._active_profile()
        if not profile or not profile.enabled:
            self.banner.set_message("Choose an enabled sorting profile before continuing.", "warning")
            self.banner.show()
            return
        if not self.sources:
            self.banner.set_message("Add at least one file or folder before continuing.", "warning")
            self.banner.show()
            return
        try:
            self.session.configure(self._current_setup(profile))
        except Exception as exc:
            self._show_error(exc)
            return
        self._update_setup_summary()
        self._show_section(self.RULES_STAGE)

    def _current_setup(self, profile: SortingProfile | None = None) -> SortSetup:
        profile = profile or self._active_profile()
        return SortSetup.create(
            profile.id if profile else "", self.sources,
            default_destination=self.default_destination.text().strip(), dry_run=self.dry_run.isChecked(),
        )

    def _update_setup_summary(self) -> None:
        if not hasattr(self, "setup_summary"):
            return
        profile = self._active_profile()
        source_count = len(self.sources)
        rule_count = len(profile.associations) if profile else 0
        ml_label = "Local suggestions on" if profile and profile.ml_enabled else "Local suggestions off"
        mode = "Dry run; no files will change" if self.dry_run.isChecked() else "Live run; approval and confirmation required"
        self.setup_summary.setText(
            f"{source_count} source selection(s) • {profile.name if profile else 'No profile'} • "
            f"{rule_count} rule(s) • {ml_label} • {mode}"
        )

    def _invalidate_plan(self, reason: str = "The setup changed.") -> None:
        if self._planning:
            self.controller.cancel()
            self.banner.set_message(f"{reason} The current review build is being cancelled safely.", "warning")
            self.banner.show()
            return
        if self._executing or self.plan is None:
            return
        self.plan = None
        self.session.invalidate()
        self.review_table.setRowCount(0)
        self.review_summary.setText("The setup changed. Build a new review before processing files.")
        self.run_button.hide()
        self.review_page.hide()
        self.banner.set_message(f"{reason} Build a new review before processing files.", "info")
        self.banner.show()

    def _start_new_run(self) -> None:
        self.session.restart()
        self.plan = None
        self.review_table.setRowCount(0)
        self.review_summary.setText("Review the setup to see proposed file changes.")
        self.run_button.hide()
        self.review_page.hide()
        self.process_page.hide()
        self.results_page.hide()
        self.banner.hide()
        self._show_section(self.SOURCE_STAGE)

    def _preview(self) -> None:
        if not self.sources:
            self.banner.set_message("Add at least one file or folder before reviewing the sort.", "warning")
            self.banner.show()
            return
        try:
            profile = self._profile_for_run()
            self.session.configure(self._current_setup(profile))
            setup = self.session.begin_preview()
        except Exception as exc:
            self._show_error(exc)
            return
        self._planning = True
        self._executing = False
        self.review_page.hide()
        self.process_page.hide()
        self.results_page.hide()
        self.preview_button.setEnabled(False)
        self.preview_progress.show()
        self.preview_progress.update_progress(0, "Building your review…", "Scanning files and reading local metadata", "0%")
        if not self.controller.preview(profile, list(setup.sources), default_destination=setup.default_destination, dry_run=setup.dry_run):
            self.session.cancel_preview()
            self._planning = False
            self.preview_button.setEnabled(True)
            self.preview_progress.hide()
            self.banner.set_message("Another sorting operation is still active. Wait for it to finish, then build the review again.", "warning")
            self.banner.show()

    def _render_plan(self) -> None:
        if not self.plan:
            return
        self._updating_table = True
        self.review_table.setRowCount(len(self.plan.items))
        for row, item in enumerate(self.plan.items):
            check = QCheckBox()
            check.setChecked(item.approved or (not item.requires_review and item.decision_source == "rule"))
            check.setEnabled(item.selected and item.action != SortAction.IGNORE)
            check.setAccessibleName(f"Approve sorting {item.metadata.name}")
            self.review_table.setCellWidget(row, 0, check)
            category = item.matched_association_name or item.category or "Not selected"
            note_parts = [item.conflict.replace("_", " ").title() if item.conflict else ""]
            note_parts.extend(item.warnings)
            values = [
                item.metadata.name,
                category,
                item.action.value.title(),
                item.destination or "—",
                "; ".join(value for value in note_parts if value) or "Ready",
            ]
            for column, value in enumerate(values, 1):
                cell = QTableWidgetItem(value)
                if column != 4:
                    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column == 1:
                    cell.setToolTip(item.metadata.path)
                elif column == 5:
                    cell.setToolTip(item.explanation)
                self.review_table.setItem(row, column, cell)
        self._updating_table = False
        review_count = sum(item.requires_review for item in self.plan.items)
        conflict_count = sum(bool(item.conflict) for item in self.plan.items)
        self.review_summary.setText(
            f"{len(self.plan.items)} file(s) • {self.plan.total_bytes:,} bytes planned • "
            f"{review_count} need review • {conflict_count} conflict(s) • "
            f"{'Dry run: no files will change.' if self.plan.dry_run else 'Live plan: confirmation is required.'}"
        )
        self.run_button.show()
        self._filter_review()

    def _approved_sources(self) -> list[str]:
        if not self.plan:
            return []
        return [item.metadata.path for row, item in enumerate(self.plan.items) if isinstance(self.review_table.cellWidget(row, 0), QCheckBox) and self.review_table.cellWidget(row, 0).isChecked()]

    def _approve_high(self) -> None:
        if not self.plan:
            return
        profile = self._active_profile()
        threshold = profile.high_confidence if profile else 0.92
        for row, item in enumerate(self.plan.items):
            check = self.review_table.cellWidget(row, 0)
            if isinstance(check, QCheckBox) and check.isEnabled() and item.confidence >= threshold and item.conflict not in {"review", "duplicate_operation", "self"}:
                check.setChecked(True)

    def _approve_visible(self) -> None:
        for row in range(self.review_table.rowCount()):
            check = self.review_table.cellWidget(row, 0)
            if not self.review_table.isRowHidden(row) and isinstance(check, QCheckBox) and check.isEnabled():
                check.setChecked(True)

    def _clear_approvals(self) -> None:
        for row in range(self.review_table.rowCount()):
            check = self.review_table.cellWidget(row, 0)
            if isinstance(check, QCheckBox) and check.isEnabled():
                check.setChecked(False)

    def _filter_review(self, *_args) -> None:
        if not self.plan:
            return
        needle = self.review_filter.text().casefold().strip()
        choice = self.confidence_filter.currentText()
        for row, item in enumerate(self.plan.items):
            text = " ".join((item.metadata.path, item.matched_association_name, item.decision_source, item.action.value, item.destination, item.conflict, *item.warnings, item.explanation)).casefold()
            visible = not needle or needle in text
            if choice == "Needs review":
                visible = visible and item.requires_review
            elif choice == "Conflicts":
                visible = visible and bool(item.conflict)
            elif choice == "Warnings":
                visible = visible and bool(item.warnings)
            self.review_table.setRowHidden(row, not visible)

    def _review_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_table or not self.plan or item.column() != 4:
            return
        try:
            original = self.plan.items[item.row()]
            changed = self.service.planner.with_manual_destination(original, item.text())
            if original.decision_source == "ml":
                target = Path(changed.destination)
                previous = MLSuggestion(
                    original.category, str(Path(original.destination).parent) if original.destination else "",
                    original.confidence, original.explanation,
                )
                self.service.planner.ml.record_correction(
                    original.metadata, previous, target.parent.name or original.category, str(target.parent),
                )
            items = list(self.plan.items)
            items[item.row()] = changed
            self.plan = replace(self.plan, items=tuple(items))
            self.session.revise_plan(self.plan)
        except Exception as exc:
            self.banner.set_message(str(exc), "warning")
            self.banner.show()
            self._render_plan()

    def _edit_destination(self) -> None:
        row = self.review_table.currentRow()
        if row < 0 or not self.plan:
            return
        value = QFileDialog.getSaveFileName(self, "Choose exact destination", self.plan.items[row].destination or self.plan.items[row].metadata.name)[0]
        if value:
            self.review_table.item(row, 4).setText(value)

    def _run(self) -> None:
        if not self.plan:
            return
        try:
            approved = list(self.session.approve(self._approved_sources()))
        except Exception as exc:
            self._show_error(exc)
            return
        confirmed = self.plan.dry_run
        if not self.plan.dry_run:
            actions = ", ".join(sorted({item.action.value.title() for item in self.plan.items if item.metadata.path in approved}))
            answer = QMessageBox.question(
                self, "Run reviewed sort",
                f"Process {len(approved)} selected file(s)?\n\nActions: {actions or 'None'}\n\nThe transaction journal will record recoverable changes.",
            )
            confirmed = answer == QMessageBox.StandardButton.Yes
            if not confirmed:
                return
        self._planning = False
        self._executing = True
        try:
            self.session.begin_processing()
        except Exception as exc:
            self._executing = False
            self._show_error(exc)
            return
        self.run_button.setEnabled(False)
        action_count = len(self.plan.items) if self.plan.dry_run else len(approved)
        mode_name = self._active_profile().name if self.use_advanced_profile.isChecked() and self._active_profile() else "Media and documents"
        self.processing_summary.setText(
            f"Processing {action_count} reviewed file(s) with {mode_name}. "
            f"{'This dry run records the plan without changing files.' if self.plan.dry_run else 'Completed operations are verified and recorded for recovery.'}"
        )
        self._show_section(self.PROCESS_STAGE)
        self.progress.update_progress(0, "Processing approved plan…", "Preparing transaction journal", "0%")
        for button in (self.pause_button, self.resume_button, self.skip_button):
            button.show()
        if not self.controller.execute(self.plan, approved, confirmed=confirmed, retry_attempts=self.retry_count.value()):
            self._executing = False
            self.session.fail_processing()
            self._show_section(self.REVIEW_STAGE)
            self.banner.set_message("Another sorting operation is still active. Wait for it to finish, then try again.", "warning")
            self.banner.show()

    def _skip_current(self) -> None:
        if not self.plan:
            return
        row = self.review_table.currentRow()
        if 0 <= row < len(self.plan.items):
            self.controller.skip_current(self.plan.items[row].metadata.path)

    def _on_progress(self, event) -> None:
        value = int((event.progress or 0) * 100)
        panel = self.preview_progress if self._planning else self.progress
        panel.update_progress(value, event.message, event.current_item or event.message, f"{value}% • {format_eta(event.eta_seconds)}")

    def _on_completed(self, result) -> None:
        self.preview_progress.hide()
        self.preview_button.setEnabled(True)
        self.run_button.setEnabled(True)
        for button in (self.pause_button, self.resume_button, self.skip_button):
            button.hide()
        if self._planning:
            self._planning = False
            candidate = result.data.get("sort_plan")
            self.session.accept_plan(candidate)
            self.plan = candidate
            self._render_plan()
            self._show_section(self.REVIEW_STAGE)
            self.banner.set_message("Review ready. No files have changed; approve the rows you want to process.", "success")
        elif self._executing:
            self._executing = False
            self.process_page.hide()
            sort_result = result.data.get("sort_result")
            if sort_result:
                self.session.complete(sort_result)
                self.operations.record(
                    "sorting", "warning" if sort_result.failed else "completed", title="Sort Files run",
                    counts={"completed": sort_result.completed, "skipped": sort_result.skipped, "errors": sort_result.failed, "verified": sort_result.verified},
                    summary={"journal_path": sort_result.journal_path, "run_id": sort_result.run_id},
                    resume_available=sort_result.undo_available, warnings=list(sort_result.failures),
                )
                self.banner.set_message(f"Sorting finished: {sort_result.completed} completed, {sort_result.skipped} skipped, {sort_result.failed} failed.", "success" if not sort_result.failed else "warning")
                self.result_summary.setText(
                    f"Latest run: {sort_result.completed} completed • {sort_result.verified} verified • "
                    f"{sort_result.skipped} skipped • {sort_result.failed} failed • "
                    f"{'Undo available' if sort_result.undo_available else 'No app-managed undo available'}"
                )
            self._refresh_history()
            if self.history_table.rowCount():
                self.history_table.selectRow(0)
            self._show_section(self.RESULTS_STAGE)
        self.banner.show()

    def _on_cancelled(self, _result) -> None:
        was_planning = self._planning
        was_executing = self._executing
        self._planning = self._executing = False
        self.preview_progress.hide()
        self.preview_button.setEnabled(True)
        self.run_button.setEnabled(True)
        self.banner.set_message("Sorting cancelled safely. Completed operations remain in the transaction journal and may be undoable.", "warning")
        self.banner.show()
        self._refresh_history()
        if was_planning:
            self.session.cancel_preview()
            self._show_section(self.RULES_STAGE)
        elif was_executing:
            self.session.cancel_processing()
            self.process_page.hide()
            self.result_summary.setText("The run was cancelled safely. Review its journal below to retry, resume, or undo completed operations.")
            self._show_section(self.RESULTS_STAGE)

    def _on_error(self, error) -> None:
        was_planning = self._planning
        was_executing = self._executing
        if was_planning:
            self.session.cancel_preview()
        elif was_executing:
            self.session.fail_processing()
            self.process_page.hide()
        self._planning = self._executing = False
        self.preview_progress.hide()
        self.preview_button.setEnabled(True)
        self.run_button.setEnabled(True)
        self.banner.set_message(getattr(error, "message", str(error)), "danger")
        self.banner.show()
        self._show_section(self.RULES_STAGE if was_planning else self.REVIEW_STAGE)

    def _new_profile(self) -> None:
        dialog = ProfileDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            profile = self.store.save(dialog.profile())
            self._refresh_profiles(profile.id)

    def _edit_profile(self) -> None:
        profile = self._active_profile()
        if not profile:
            return
        dialog = ProfileDialog(profile, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.store.save(dialog.profile())
            self._refresh_profiles(profile.id)

    def _duplicate_profile(self) -> None:
        profile = self._active_profile()
        if profile:
            copy = self.store.duplicate(profile.id)
            self._refresh_profiles(copy.id)

    def _import_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import sorting profile", filter="JSON files (*.json)")
        if path:
            try:
                profile = self.store.import_profile(path)
                self._refresh_profiles(profile.id)
            except Exception as exc:
                self._show_error(exc)

    def _export_profile(self) -> None:
        profile = self._active_profile()
        if not profile:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export sorting profile", f"{profile.name}.json", "JSON files (*.json)")
        if path:
            try:
                self.store.export_profile(profile.id, path)
                self.banner.set_message("Sorting profile exported.", "success")
                self.banner.show()
            except Exception as exc:
                self._show_error(exc)

    def _toggle_profile(self) -> None:
        profile = self._active_profile()
        if profile:
            self.store.set_enabled(profile.id, not profile.enabled)
            self._refresh_profiles(profile.id)

    def _delete_profile(self) -> None:
        profile = self._active_profile()
        if not profile or len(self.store.list()) <= 1:
            self.banner.set_message("Keep at least one sorting profile.", "warning")
            self.banner.show()
            return
        if QMessageBox.question(self, "Delete sorting profile", f"Delete “{profile.name}”? This does not change any files.") == QMessageBox.StandardButton.Yes:
            self.store.delete(profile.id)
            self._refresh_profiles()

    def _add_association(self) -> None:
        profile = self._active_profile()
        if not profile:
            return
        dialog = AssociationDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.store.save(replace(profile, associations=(*profile.associations, dialog.association())))
            self._refresh_profiles(profile.id)

    def _edit_association(self) -> None:
        profile = self._active_profile()
        row = self.association_table.currentRow()
        if not profile or row < 0:
            return
        dialog = AssociationDialog(profile.associations[row], self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            associations = list(profile.associations)
            associations[row] = dialog.association()
            self.store.save(replace(profile, associations=tuple(associations)))
            self._refresh_profiles(profile.id)

    def _remove_association(self) -> None:
        profile = self._active_profile()
        row = self.association_table.currentRow()
        if profile and row >= 0:
            associations = tuple(value for index, value in enumerate(profile.associations) if index != row)
            self.store.save(replace(profile, associations=associations))
            self._refresh_profiles(profile.id)

    def _render_associations(self, profile: SortingProfile) -> None:
        self._association_rows = list(profile.associations)
        enabled = sum(association.enabled for association in self._association_rows)
        if enabled:
            self.rules_status.setText(
                f"{enabled} enabled deterministic rule(s). Higher priority wins; equal-priority matches are sent to Review."
            )
        elif profile.ml_enabled:
            self.rules_status.setText(
                "No deterministic rules are enabled. Files will use local suggestions and remain subject to Review."
            )
        else:
            self.rules_status.setText(
                "No rules are enabled and local suggestions are off. Unmatched files will remain unassigned in Review."
            )
        self.association_table.setRowCount(len(self._association_rows))
        for row, association in enumerate(self._association_rows):
            values = ["Yes" if association.enabled else "No", str(association.priority), association.name, str(len(association.conditions)), association.action.value.title(), association.destination or "—", association.conflict_policy.value.replace("_", " ").title()]
            for column, value in enumerate(values):
                self.association_table.setItem(row, column, QTableWidgetItem(value))

    def _add_monitor(self) -> None:
        profile = self._active_profile()
        if not profile:
            return
        dialog = MonitorDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            monitor = dialog.monitor()
            saved = self.store.save(replace(profile, monitored_folders=(*profile.monitored_folders, monitor)))
            self._configure_monitor(saved, monitor)
            self._refresh_profiles(profile.id)

    def _edit_monitor(self) -> None:
        profile = self._active_profile()
        row = self.monitor_table.currentRow()
        if not profile or row < 0:
            return
        dialog = MonitorDialog(profile.monitored_folders[row], self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            monitor = dialog.monitor()
            monitors = list(profile.monitored_folders)
            monitors[row] = monitor
            saved = self.store.save(replace(profile, monitored_folders=tuple(monitors)))
            self._configure_monitor(saved, monitor)
            self._refresh_profiles(profile.id)

    def _configure_monitor(self, profile: SortingProfile, monitor: MonitoredFolder) -> None:
        if monitor.scan_mode != "scheduled":
            return
        try:
            self.scheduler.configure(profile, monitor, data_root=str(self.paths.root))
        except Exception as exc:
            self._show_error(exc)

    def _remove_monitor(self) -> None:
        profile = self._active_profile()
        row = self.monitor_table.currentRow()
        if profile and row >= 0:
            monitors = tuple(value for index, value in enumerate(profile.monitored_folders) if index != row)
            self.store.save(replace(profile, monitored_folders=monitors))
            self._refresh_profiles(profile.id)

    def _poll_monitor(self) -> None:
        profile = self._active_profile()
        row = self.monitor_table.currentRow()
        if not profile or row < 0:
            return
        try:
            changed = self.monitor.poll(profile.monitored_folders[row])
            self._add_sources([item.path for item in changed])
            self._show_section(self.SOURCE_STAGE)
            self.banner.set_message(f"Found {len(changed)} new or changed file(s) in the monitored folder.", "success")
            self.banner.show()
        except Exception as exc:
            self._show_error(exc)

    def _poll_change_monitors(self) -> None:
        profile = self._active_profile()
        if not profile:
            return
        added: list[str] = []
        for monitor in profile.monitored_folders:
            if not monitor.enabled or monitor.scan_mode != "filesystem_change":
                continue
            try:
                added.extend(item.path for item in self.monitor.poll(monitor))
            except Exception:
                continue
        if added:
            self._add_sources(added)
            self.banner.set_message(f"Queued {len(added)} new or changed monitored file(s) for review.", "info")
            self.banner.show()

    def _render_monitors(self, profile: SortingProfile) -> None:
        self.monitor_table.setRowCount(len(profile.monitored_folders))
        for row, monitor in enumerate(profile.monitored_folders):
            values = ["Yes" if monitor.enabled else "No", monitor.path, monitor.scan_mode.replace("_", " ").title(), monitor.schedule.title(), "Dry run" if monitor.dry_run else "Live approved" if monitor.live_approved else "Approval required"]
            for column, value in enumerate(values):
                self.monitor_table.setItem(row, column, QTableWidgetItem(value))

    def _refresh_history(self) -> None:
        self.runs = self.executor.list_runs()
        self.history_table.setRowCount(len(self.runs))
        for row, run in enumerate(self.runs):
            records = run.get("records", [])
            completed = sum(record.get("status") == "completed" for record in records)
            failed = sum(record.get("status") == "failed" for record in records)
            undo = any(record.get("status") == "completed" and not record.get("undone_at") and record.get("action") != "recycle" for record in records)
            values = [run.get("run_id", ""), run.get("status", ""), run.get("created_at", ""), str(completed), str(failed), "Available" if undo else "—"]
            for column, value in enumerate(values):
                self.history_table.setItem(row, column, QTableWidgetItem(value))
        self._refresh_stats()

    def _selected_run(self) -> dict | None:
        row = self.history_table.currentRow()
        return self.runs[row] if 0 <= row < len(getattr(self, "runs", [])) else None

    def _history_selected(self) -> None:
        run = self._selected_run()
        if not run:
            self.history_detail.setText("Select a run to inspect its operation log.")
            return
        records = run.get("records", [])
        lines = [f"{record.get('status', '')}: {Path(record.get('source', '')).name} → {record.get('destination') or record.get('action', '')}" for record in records[:12]]
        self.history_detail.setText("\n".join(lines) or "This run contains no processed records.")

    def _undo_selected(self) -> None:
        run = self._selected_run()
        if not run:
            return
        if QMessageBox.question(self, "Undo sorting run", "Restore recoverable operations from this run? Recycle Bin actions cannot be undone here.") != QMessageBox.StandardButton.Yes:
            return
        result = self.executor.undo(str(run.get("run_id", "")))
        self.operations.record("sorting_undo", "warning" if result.failed else "completed", title="Undo Sort Files run", counts={"restored": result.completed, "skipped": result.skipped, "errors": result.failed}, summary={"journal_path": result.journal_path}, failures=list(result.failures))
        self.banner.set_message(f"Undo finished: {result.completed} restored, {result.skipped} skipped, {result.failed} failed.", "success" if not result.failed else "warning")
        self.banner.show()
        self._refresh_history()

    def _retry_selected(self) -> None:
        run = self._selected_run()
        if not run:
            return
        run_id = str(run.get("run_id", ""))
        if QMessageBox.question(self, "Retry sorting run", "Retry failed or interrupted files after rechecking the reviewed source metadata?") != QMessageBox.StandardButton.Yes:
            return
        try:
            has_failures = any(record.get("status") == "failed" for record in run.get("records", []))
            result = self.executor.retry_failed(run_id, confirmed=True, retry_attempts=self.retry_count.value()) if has_failures else self.executor.resume_run(run_id, confirmed=True, retry_attempts=self.retry_count.value())
            self.operations.record("sorting_retry", "warning" if result.failed else "completed", title="Retry Sort Files run", counts={"completed": result.completed, "skipped": result.skipped, "errors": result.failed}, summary={"journal_path": result.journal_path, "run_id": result.run_id}, failures=list(result.failures), resume_available=result.undo_available)
            self.banner.set_message(f"Retry finished: {result.completed} completed, {result.failed} failed.", "success" if not result.failed else "warning")
            self.banner.show()
            self._refresh_history()
        except Exception as exc:
            self._show_error(exc)

    def _open_history_path(self, field: str) -> None:
        run = self._selected_run()
        if not run or not run.get("records"):
            return
        value = str(run["records"][0].get(field, ""))
        if value:
            path = Path(value)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent if path.suffix else path)))

    def _open_history_report(self) -> None:
        run = self._selected_run()
        if run:
            path = self.paths.sorting / "runs" / str(run.get("run_id", "")) / "journal.json"
            if path.is_file():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _export_history_report(self) -> None:
        run = self._selected_run()
        if not run:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export sorting report", f"{run.get('run_id', 'sorting-report')}.json", "JSON (*.json);;CSV (*.csv)")
        if not path:
            return
        try:
            self.executor.export_run(str(run.get("run_id", "")), path)
            self.banner.set_message("Sorting report exported.", "success")
            self.banner.show()
        except Exception as exc:
            self._show_error(exc)

    def _refresh_stats(self) -> None:
        runs = self.executor.list_runs()
        records = [record for run in runs for record in run.get("records", [])]
        profile = self._active_profile()
        self.stat_runs.value_label.setText(str(len(runs)))
        self.stat_files.value_label.setText(str(sum(record.get("status") == "completed" for record in records)))
        self.stat_monitors.value_label.setText(str(sum(folder.enabled for folder in profile.monitored_folders) if profile else 0))
        self.stat_undo.value_label.setText(str(sum(any(record.get("status") == "completed" and not record.get("undone_at") and record.get("action") != "recycle" for record in run.get("records", [])) for run in runs)))

    def _show_error(self, exc: Exception) -> None:
        self.banner.set_message(getattr(getattr(exc, "error", None), "message", str(exc)), "danger")
        self.banner.show()
