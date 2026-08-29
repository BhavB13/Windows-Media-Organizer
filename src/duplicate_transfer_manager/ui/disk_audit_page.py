"""Native, read-only Disk Audit page."""

from __future__ import annotations

import os
import subprocess
import json
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..controllers import DiskAuditController
from ..core import (
    AppSettings,
    AuditTier,
    BackupStatus,
    DiskAuditItem,
    DiskAuditSettings,
    DiskAuditSnapshot,
)
from ..runtime_paths import RuntimePaths, get_runtime_paths
from ..services import AuditSnapshotStore, DiskAuditService, OperationRecordService, SettingsService, compare_snapshots
from .pages import BasePage
from .theme import Spacing
from .widgets import (
    Card,
    DisclosurePanel,
    EmptyState,
    InlineMessage,
    MetricCard,
    PrimaryButton,
    ProgressPanel,
    ResponsiveGrid,
    SectionHeader,
    ToastBanner,
    format_eta,
)
from ..services import format_duplicate_size


TIER_LABELS = {
    AuditTier.PROTECT_RECLAIM: "Tier 1 — Protect & reclaim",
    AuditTier.REGENERABLE_CACHE: "Tier 2 — Regenerable caches",
    AuditTier.APPS_REVIEW: "Tier 3 — Apps to review",
    AuditTier.WINDOWS_MANAGED: "Tier 4 — Windows-managed storage",
}

TIER_DESCRIPTIONS = {
    AuditTier.PROTECT_RECLAIM: "Large user-controlled data. Review value and backup status before removing anything.",
    AuditTier.REGENERABLE_CACHE: "Allowlisted cache locations that can regenerate, usually at the cost of download or rebuild time.",
    AuditTier.APPS_REVIEW: "Installed applications to review. The audit never claims that an app is unused.",
    AuditTier.WINDOWS_MANAGED: "Operating-system storage. Use supported Windows controls and never delete these paths directly.",
}


def open_audit_path(path: str) -> bool:
    """Open a folder or select a file without changing it."""

    candidate = Path(path)
    if not candidate.exists():
        return False
    if os.name == "nt" and candidate.is_file():
        # Explorer's documented switch includes a comma delimiter. Passing it
        # as its own argument is required on current Windows 11 builds; folding
        # the path into the switch can open Explorer without selecting the file.
        subprocess.Popen(["explorer.exe", "/select,", str(candidate)])
        return True
    target = candidate if candidate.is_dir() else candidate.parent
    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))))


class FindingCard(Card):
    open_requested = Signal(str)
    copy_requested = Signal(str)
    command_requested = Signal(str)
    reviewed_requested = Signal(str, bool)
    hidden_requested = Signal(str, bool)
    backup_requested = Signal(str, str)

    def __init__(self, item: DiskAuditItem, state: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent, subtle=True)
        self.item = item
        layout = QVBoxLayout(self)
        layout.setContentsMargins(Spacing.LG, Spacing.MD, Spacing.LG, Spacing.MD)
        layout.setSpacing(Spacing.SM)

        identity_widget = QWidget()
        identity = QVBoxLayout(identity_widget)
        identity.setContentsMargins(0, 0, 0, 0)
        identity.setSpacing(Spacing.XS)
        name = QLabel(item.label)
        name.setProperty("role", "section")
        name.setWordWrap(True)
        size = QLabel(format_duplicate_size(item.size_bytes))
        size.setProperty("role", "section")
        identity.addWidget(name)
        identity.addWidget(size)
        identity.addStretch()

        status_parts = [item.verdict.replace("_", " ").title(), item.measurement_confidence.title()]
        backup = state.get("backup_status", BackupStatus.UNKNOWN.value)
        if item.tier == AuditTier.PROTECT_RECLAIM:
            status_parts.append("Backup confirmed by you" if backup == BackupStatus.USER_CONFIRMED.value else "Backup status unknown")
        if state.get("reviewed"):
            status_parts.append("Reviewed")
        details_widget = QWidget()
        details = QVBoxLayout(details_widget)
        details.setContentsMargins(0, 0, 0, 0)
        details.setSpacing(Spacing.SM)
        status = QLabel(" • ".join(status_parts))
        status.setProperty("muted", True)
        status.setWordWrap(True)
        details.addWidget(status)
        for path in item.paths:
            path_label = QLabel(path)
            path_label.setProperty("muted", True)
            path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            path_label.setWordWrap(True)
            details.addWidget(path_label)
        if item.warnings:
            warning = QLabel(f"Measurement note: {item.warnings[0]}")
            warning.setProperty("status", "warning")
            warning.setWordWrap(True)
            details.addWidget(warning)

        guidance_widget = QWidget()
        guidance = QVBoxLayout(guidance_widget)
        guidance.setContentsMargins(0, 0, 0, 0)
        reason = QLabel(f"{item.rationale}\n{item.recommendation}")
        reason.setWordWrap(True)
        guidance.addWidget(reason)

        action_widget = QWidget()
        actions = QVBoxLayout(action_widget)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(Spacing.XS)
        for path in item.paths:
            open_button = QPushButton("Open in Explorer")
            open_button.setAccessibleName(f"Open {path} in Explorer")
            open_button.clicked.connect(lambda _checked=False, value=path: self.open_requested.emit(value))
            actions.addWidget(open_button)
            copy_button = QPushButton("Copy path")
            copy_button.clicked.connect(lambda _checked=False, value=path: self.copy_requested.emit(value))
            actions.addWidget(copy_button)
        if item.tier == AuditTier.APPS_REVIEW:
            apps_button = QPushButton("Open Apps settings")
            apps_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("ms-settings:appsfeatures")))
            actions.addWidget(apps_button)
        if item.command:
            command = QPushButton("Copy command")
            command.clicked.connect(lambda: self.command_requested.emit(item.command))
            actions.addWidget(command)
        reviewed = QCheckBox("Reviewed")
        reviewed.setChecked(bool(state.get("reviewed", False)))
        reviewed.toggled.connect(lambda checked: self.reviewed_requested.emit(item.stable_id, checked))
        hidden = QCheckBox("Hide from recommendations")
        hidden.setChecked(bool(state.get("hidden", False)))
        hidden.toggled.connect(lambda checked: self.hidden_requested.emit(item.stable_id, checked))
        actions.addWidget(reviewed)
        actions.addWidget(hidden)
        if item.tier == AuditTier.PROTECT_RECLAIM:
            backed_up = QCheckBox("I have a second copy")
            backed_up.setChecked(backup == BackupStatus.USER_CONFIRMED.value)
            backed_up.setToolTip("This is your declaration; Disk Audit does not hash-verify the backup.")
            backed_up.toggled.connect(
                lambda checked: self.backup_requested.emit(
                    item.stable_id,
                    BackupStatus.USER_CONFIRMED.value if checked else BackupStatus.UNKNOWN.value,
                )
            )
            actions.addWidget(backed_up)
        actions.addStretch()
        layout.addWidget(
            ResponsiveGrid(
                [identity_widget, details_widget, guidance_widget, action_widget],
                min_column_width=230,
                max_columns=4,
            )
        )


class DiskAuditPage(BasePage):
    """Drive selection, background scanning, comparison, and tiered findings."""

    def __init__(
        self,
        paths: RuntimePaths | None = None,
        settings: AppSettings | None = None,
        settings_service: SettingsService | None = None,
        operation_service: OperationRecordService | None = None,
        service: DiskAuditService | None = None,
        controller: DiskAuditController | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "Disk Audit",
            "See what uses a local drive, what changed, and what deserves attention—without changing any files.",
            parent,
        )
        self.paths = paths or get_runtime_paths()
        self.settings = settings or AppSettings()
        self.settings_service = settings_service or SettingsService(self.paths)
        self.operations = operation_service or OperationRecordService(self.paths)
        self.service = service or DiskAuditService(self.paths)
        self.store: AuditSnapshotStore = self.service.store
        self.controller = controller or DiskAuditController(self.service)
        self.snapshot: DiskAuditSnapshot | None = None
        self.comparison: dict = {}
        self._active_settings: DiskAuditSettings | None = None
        self._tier_panels: list[DisclosurePanel] = []

        self._build_setup()
        self.progress_panel = ProgressPanel()
        self.progress_panel.hide()
        self.progress_panel.cancel_requested.connect(self.controller.cancel)
        self.content.addWidget(self.progress_panel)

        self.banner = ToastBanner("Disk Audit messages appear here.", "info")
        self.banner.hide()
        self.content.addWidget(self.banner)

        self.results = QWidget()
        self.results_layout = QVBoxLayout(self.results)
        self.results_layout.setContentsMargins(0, 0, 0, 0)
        self.results_layout.setSpacing(Spacing.LG)
        self.content.addWidget(self.results)
        self.empty = EmptyState(
            "No disk audit yet",
            "Choose a local drive and run a Quick audit. Deep mode also discovers large unclassified files and folders.",
            "Run Quick audit",
            "drive",
        )
        self.empty.action_requested.connect(self.start_audit)
        self.results_layout.addWidget(self.empty)

        self.controller.progress.connect(self._on_progress)
        self.controller.completed.connect(self._on_completed)
        self.controller.cancelled.connect(self._on_cancelled)
        self.controller.failed.connect(self._on_failed)
        self.controller.recoverable_error.connect(self._on_recoverable)
        self.controller.busy_changed.connect(lambda busy: self.run_button.setEnabled(not busy))
        self.refresh_drives()
        QTimer.singleShot(0, self.refresh)
        self.finish()

    def _build_setup(self) -> None:
        setup = Card()
        layout = QVBoxLayout(setup)
        layout.setContentsMargins(Spacing.LG, Spacing.LG, Spacing.LG, Spacing.LG)
        layout.setSpacing(Spacing.MD)
        layout.addWidget(SectionHeader("Choose what to audit"))
        row = QHBoxLayout()
        self.drive = QComboBox()
        self.drive.setMinimumWidth(260)
        self.drive.setAccessibleName("Local drive to audit")
        self.drive.currentIndexChanged.connect(self._selection_changed)
        self.mode = QComboBox()
        self.mode.addItem("Quick — known high-value locations", "quick")
        self.mode.addItem("Deep — full drive discovery", "deep")
        expected_mode = self.mode.findData(self.settings.last_audit_mode)
        if expected_mode >= 0:
            self.mode.setCurrentIndex(expected_mode)
        self.mode.currentIndexChanged.connect(self._selection_changed)
        refresh = QPushButton("Refresh drives")
        refresh.clicked.connect(self.refresh_drives)
        self.run_button = PrimaryButton("Run audit")
        self.run_button.clicked.connect(self.start_audit)
        row.addWidget(QLabel("Drive"))
        row.addWidget(self.drive, 1)
        row.addWidget(QLabel("Mode"))
        row.addWidget(self.mode)
        row.addWidget(refresh)
        row.addWidget(self.run_button)
        layout.addLayout(row)
        self.last_audit = QLabel("No previous audit for this selection.")
        self.last_audit.setProperty("muted", True)
        layout.addWidget(self.last_audit)
        safety = InlineMessage(
            "Read-only: Disk Audit measures and recommends. It never deletes, cleans, uninstalls, elevates, or runs maintenance commands.",
            "success",
        )
        layout.addWidget(safety)
        self.content.addWidget(setup)

    def refresh_drives(self) -> None:
        current = self.drive.currentData() or self.settings.last_audit_drive
        self.drive.blockSignals(True)
        self.drive.clear()
        try:
            volumes = self.service.list_volumes()
        except Exception as exc:  # noqa: BLE001 - unavailable drives should not break the page
            volumes = []
            self._message(f"Could not list local drives: {exc}", "danger")
        for volume in volumes:
            free = format_duplicate_size(int(volume.get("free_bytes", 0)))
            label = volume.get("label") or volume.get("root")
            self.drive.addItem(f"{label} ({volume.get('root')}) — {free} free", volume.get("root"))
        selected = self.drive.findData(current)
        if selected >= 0:
            self.drive.setCurrentIndex(selected)
        self.drive.blockSignals(False)
        self._selection_changed()

    def _selection_changed(self) -> None:
        root = str(self.drive.currentData() or "")
        mode = str(self.mode.currentData() or "quick")
        latest = None
        for volume in self.service.list_volumes() if root else []:
            if volume.get("root") == root:
                latest = self.store.latest(volume_id=str(volume.get("volume_id", "")), mode=mode)
                break
        self.last_audit.setText(
            f"Last {mode} audit: {latest.completed_at[:19].replace('T', ' ')}"
            if latest
            else f"No previous {mode} audit for this drive."
        )

    def start_audit(self) -> None:
        root = str(self.drive.currentData() or "")
        if not root:
            self._message("Choose an available local drive first.", "warning")
            return
        mode = str(self.mode.currentData() or "quick")
        self._active_settings = DiskAuditSettings(root, mode)
        self.settings.last_audit_drive = root
        self.settings.last_audit_mode = mode
        self.settings_service.save(self.settings)
        self.progress_panel.show()
        self.progress_panel.progress.setRange(0, 0)
        self.progress_panel.title.setText("Starting Disk Audit…")
        self.progress_panel.detail.setText(root)
        self.banner.hide()
        if not self.controller.start(self._active_settings):
            self.progress_panel.hide()

    def _on_progress(self, event) -> None:
        if event.progress is None:
            self.progress_panel.progress.setRange(0, 0)
            value = 0
        else:
            self.progress_panel.progress.setRange(0, 100)
            value = int(event.progress * 100)
        metrics = f"{event.processed_items:,} items • ETA {format_eta(event.eta_seconds)}"
        self.progress_panel.update_progress(value, event.message or "Auditing…", event.current_item or "Read-only scan", metrics)

    def _on_completed(self, result) -> None:
        self.progress_panel.hide()
        snapshot = result.data.get("snapshot")
        if not isinstance(snapshot, DiskAuditSnapshot):
            self._message("The audit completed without a readable snapshot.", "danger")
            return
        self.operations.record(
            "disk_audit",
            "completed",
            title=f"{snapshot.mode.title()} disk audit — {snapshot.volume_label or snapshot.scanned_root}",
            counts=dict(result.counts),
            summary={
                "audit_id": snapshot.audit_id,
                "volume_id": snapshot.volume_id,
                "mode": snapshot.mode,
                "snapshot_path": result.report_path,
            },
            warnings=list(result.warnings),
        )
        self.load_snapshot(snapshot, result.data.get("comparison", {}))
        self._message("Disk audit complete. Results and review state stay local.", "success")

    def _on_cancelled(self, result) -> None:
        self.progress_panel.hide()
        settings = self._active_settings
        self.operations.record(
            "disk_audit",
            "cancelled",
            title="Cancelled disk audit",
            summary={"volume": settings.volume_root if settings else "", "mode": settings.mode if settings else ""},
        )
        self._message("Disk audit cancelled. No comparison baseline was replaced.", "warning")

    def _on_failed(self, error) -> None:
        self.progress_panel.hide()
        settings = self._active_settings
        message = getattr(error, "message", str(error))
        self.operations.record(
            "disk_audit",
            "failed",
            title="Failed disk audit",
            summary={"volume": settings.volume_root if settings else "", "mode": settings.mode if settings else ""},
            failures=[message],
        )
        self._message(message, "danger")

    def _on_recoverable(self, error) -> None:
        self._message(getattr(error, "message", str(error)), "warning")

    def refresh(self) -> None:
        if self.controller.busy:
            return
        latest = self.store.latest()
        if latest is not None and (self.snapshot is None or latest.audit_id != self.snapshot.audit_id):
            previous = None
            if latest.previous_snapshot_id:
                try:
                    previous = self.store.load(latest.previous_snapshot_id)
                except (OSError, ValueError, json.JSONDecodeError):
                    previous = None
            self.load_snapshot(latest, compare_snapshots(latest, previous))
        self._selection_changed()

    def load_snapshot(self, snapshot: DiskAuditSnapshot, comparison: dict | None = None) -> None:
        self.snapshot = snapshot
        self.comparison = comparison or {}
        self._render_results()

    def load_snapshot_path(self, path: str) -> None:
        try:
            snapshot = self.store.load(path)
        except Exception as exc:  # noqa: BLE001
            self._message(f"Could not load audit results: {exc}", "danger")
            return
        previous = None
        if snapshot.previous_snapshot_id:
            try:
                previous = self.store.load(snapshot.previous_snapshot_id)
            except (OSError, ValueError, json.JSONDecodeError):
                previous = None
        self.load_snapshot(snapshot, compare_snapshots(snapshot, previous))

    def _render_results(self) -> None:
        previous_query = self.search.text() if hasattr(self, "search") else ""
        previous_show_hidden = self.show_hidden.isChecked() if hasattr(self, "show_hidden") else False
        while self.results_layout.count():
            item = self.results_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if self.snapshot is None:
            self.results_layout.addWidget(self.empty)
            return
        snapshot = self.snapshot
        free_percent = (snapshot.free_bytes / snapshot.capacity_bytes * 100) if snapshot.capacity_bytes else 0
        free_delta = int(self.comparison.get("free_delta", 0))
        if self.comparison.get("baseline_available") and free_delta > 0:
            headline = f"You gained {format_duplicate_size(free_delta)} of free space since the last {snapshot.mode} audit."
        elif free_percent < 10:
            headline = "This drive is running low on free space. Review the largest safe opportunities first."
        else:
            headline = "Drive audit complete. Start with protect-first items, then review regenerable storage."
        hero = Card()
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(Spacing.LG, Spacing.LG, Spacing.LG, Spacing.LG)
        title = QLabel(headline)
        title.setProperty("role", "title")
        title.setWordWrap(True)
        hero_layout.addWidget(title)
        meter = QProgressBar()
        meter.setRange(0, 1000)
        meter.setValue(int((snapshot.used_bytes / snapshot.capacity_bytes * 1000) if snapshot.capacity_bytes else 0))
        meter.setFormat(f"{format_duplicate_size(snapshot.used_bytes)} used • {format_duplicate_size(snapshot.free_bytes)} free ({free_percent:.1f}%)")
        meter.setAccessibleName("Drive used and free space")
        hero_layout.addWidget(meter)
        detail = QLabel(f"{snapshot.mode.title()} audit • {snapshot.completed_at[:19].replace('T', ' ')} • {len(snapshot.items)} findings • {snapshot.inaccessible_count} inaccessible locations")
        detail.setProperty("muted", True)
        hero_layout.addWidget(detail)
        self.results_layout.addWidget(hero)

        changes = list(self.comparison.get("changes", ()))[:8]
        if self.comparison.get("baseline_available"):
            free_delta = int(self.comparison.get("free_delta", 0))
            before_free = max(0, snapshot.free_bytes - free_delta)
            cards = [
                MetricCard(
                    ("+" if free_delta > 0 else "−" if free_delta < 0 else "") + format_duplicate_size(abs(free_delta)),
                    "Authoritative free-space change",
                    f"{format_duplicate_size(before_free)} → {format_duplicate_size(snapshot.free_bytes)} free",
                )
            ] + [
                MetricCard(
                    ("+" if int(change["delta_bytes"]) > 0 else "−") + format_duplicate_size(abs(int(change["delta_bytes"]))),
                    str(change["label"]),
                    f"{format_duplicate_size(int(change['before_bytes']))} → {format_duplicate_size(int(change['after_bytes']))}",
                )
                for change in changes
            ]
            if cards:
                self.results_layout.addWidget(SectionHeader("Changed since the last matching audit"))
                self.results_layout.addWidget(ResponsiveGrid(cards, min_column_width=230, max_columns=4))

        attention = self._attention_item()
        if attention is not None:
            state = self.store.review_state(snapshot.volume_id, attention.stable_id)
            backup_text = "Backup confirmed by you" if state.get("backup_status") == BackupStatus.USER_CONFIRMED.value else "Backup status unknown"
            warning = InlineMessage(
                f"Protect first: {attention.label} uses {format_duplicate_size(attention.size_bytes)}. {backup_text}. Open and review it before considering any removal.",
                "warning",
            )
            self.results_layout.addWidget(warning)

        toolbar = Card(subtle=True)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(Spacing.LG, Spacing.MD, Spacing.LG, Spacing.MD)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search findings")
        self.search.setText(previous_query)
        self.search.textChanged.connect(self._render_tiers)
        self.show_hidden = QCheckBox("Show hidden")
        self.show_hidden.setChecked(previous_show_hidden)
        self.show_hidden.toggled.connect(self._render_tiers)
        toolbar_layout.addWidget(QLabel("Find"))
        toolbar_layout.addWidget(self.search, 1)
        toolbar_layout.addWidget(self.show_hidden)
        self.results_layout.addWidget(toolbar)
        self.tier_host = QWidget()
        self.tier_layout = QVBoxLayout(self.tier_host)
        self.tier_layout.setContentsMargins(0, 0, 0, 0)
        self.tier_layout.setSpacing(Spacing.LG)
        self.results_layout.addWidget(self.tier_host)
        self._render_tiers()

        next_steps = DisclosurePanel("Recommended next steps and scan method")
        priorities = sorted(
            snapshot.items,
            key=lambda item: (
                list(AuditTier).index(item.tier),
                -item.size_bytes,
            ),
        )[:5]
        for index, item in enumerate(priorities, 1):
            step = QLabel(f"{index}. {item.label} ({format_duplicate_size(item.size_bytes)}): {item.recommendation}")
            step.setWordWrap(True)
            next_steps.body_layout.addWidget(step)
        for tier in AuditTier:
            count = len([item for item in snapshot.items if item.tier == tier])
            label = QLabel(f"{TIER_LABELS[tier]}: {count} finding(s). {TIER_DESCRIPTIONS[tier]}")
            label.setWordWrap(True)
            next_steps.body_layout.addWidget(label)
        method = QLabel(
            "Quick mode measures curated locations. Deep mode also walks the selected volume. "
            "Reparse points are never followed; inaccessible or disappearing paths are reported and are not treated as cleared. "
            "Commands are copied only and backup status is user-declared, not hash-verified."
        )
        method.setWordWrap(True)
        next_steps.body_layout.addWidget(method)
        self.results_layout.addWidget(next_steps)

    def _attention_item(self) -> DiskAuditItem | None:
        if self.snapshot is None:
            return None
        candidates = []
        for item in self.snapshot.items:
            state = self.store.review_state(self.snapshot.volume_id, item.stable_id)
            if item.tier == AuditTier.PROTECT_RECLAIM and not state.get("hidden") and state.get("backup_status", "unknown") != BackupStatus.USER_CONFIRMED.value:
                candidates.append(item)
        return max(candidates, key=lambda item: item.size_bytes, default=None)

    def _render_tiers(self) -> None:
        if not hasattr(self, "tier_layout") or self.snapshot is None:
            return
        while self.tier_layout.count():
            entry = self.tier_layout.takeAt(0)
            if entry.widget():
                entry.widget().deleteLater()
        query = self.search.text().casefold().strip() if hasattr(self, "search") else ""
        show_hidden = self.show_hidden.isChecked() if hasattr(self, "show_hidden") else False
        for tier in AuditTier:
            values = []
            for item in sorted((value for value in self.snapshot.items if value.tier == tier), key=lambda value: value.size_bytes, reverse=True):
                state = self.store.review_state(self.snapshot.volume_id, item.stable_id)
                haystack = " ".join((item.label, item.rationale, item.recommendation, *item.paths)).casefold()
                if query and query not in haystack:
                    continue
                if state.get("hidden") and not show_hidden:
                    continue
                values.append((item, state))
            total_size = sum(item.size_bytes for item, _state in values)
            panel = DisclosurePanel(
                f"{TIER_LABELS[tier]} — {len(values)} shown • {format_duplicate_size(total_size)} measured"
            )
            description = QLabel(TIER_DESCRIPTIONS[tier])
            description.setWordWrap(True)
            description.setProperty("muted", True)
            panel.body_layout.addWidget(description)
            copy_all = QPushButton("Copy all paths in this tier")
            copy_all.clicked.connect(lambda _checked=False, selected=tier: self._copy_tier(selected))
            panel.body_layout.addWidget(copy_all, alignment=Qt.AlignmentFlag.AlignLeft)
            for item, state in values:
                card = FindingCard(item, state)
                card.open_requested.connect(self._open_path)
                card.copy_requested.connect(self._copy_text)
                card.command_requested.connect(self._copy_text)
                card.reviewed_requested.connect(self._set_reviewed)
                card.hidden_requested.connect(self._set_hidden)
                card.backup_requested.connect(self._set_backup)
                panel.body_layout.addWidget(card)
            if not values:
                none = QLabel("No findings match the current filters.")
                none.setProperty("muted", True)
                panel.body_layout.addWidget(none)
            panel.set_expanded(tier == AuditTier.PROTECT_RECLAIM)
            self.tier_layout.addWidget(panel)
        self.tier_layout.addStretch(1)

    def _open_path(self, path: str) -> None:
        try:
            opened = open_audit_path(path)
        except OSError as exc:
            self._message(f"Could not open {path}: {exc}", "danger")
            return
        self._message(f"Opened {path}" if opened else f"Path is no longer available: {path}", "success" if opened else "warning")

    def _copy_text(self, value: str) -> None:
        QApplication.clipboard().setText(value)
        self._message("Copied to the clipboard. Nothing was executed.", "success")

    def _copy_tier(self, tier: AuditTier) -> None:
        if self.snapshot is None:
            return
        paths = [path for item in self.snapshot.items if item.tier == tier for path in item.paths]
        self._copy_text("\n".join(paths))

    def _set_reviewed(self, item_id: str, value: bool) -> None:
        if self.snapshot:
            self.store.set_review_state(self.snapshot.volume_id, item_id, reviewed=value)
            self._render_tiers()

    def _set_hidden(self, item_id: str, value: bool) -> None:
        if self.snapshot:
            self.store.set_review_state(self.snapshot.volume_id, item_id, hidden=value)
            self._render_results()

    def _set_backup(self, item_id: str, value: str) -> None:
        if self.snapshot:
            self.store.set_review_state(self.snapshot.volume_id, item_id, backup_status=value)
            self._render_results()

    def _message(self, text: str, kind: str) -> None:
        self.banner.set_message(text, kind)
        self.banner.show()
