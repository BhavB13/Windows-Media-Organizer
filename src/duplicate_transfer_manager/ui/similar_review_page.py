"""Review photos that look alike and quarantine the copies the user picks."""

from __future__ import annotations

import dataclasses
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..runtime_paths import RuntimePaths, get_runtime_paths
from ..services.duplicate_workflow import (
    DuplicateGroup,
    DuplicateReview,
    format_duplicate_size,
)
from ..services.similar_service import (
    DEFAULT_SENSITIVITY,
    SimilarPhotoGroup,
    SimilarPhotoReview,
    SimilarPhotoService,
)
from .pages import BasePage
from .theme import Spacing
from .widgets import (
    Card,
    ConfirmationDialog,
    EmptyState,
    PathSelector,
    PrimaryButton,
    ProgressPanel,
    SectionHeader,
    ToastBanner,
    set_accessible,
)

# Rendering every group of a very large library at once would stall the window;
# the first groups are shown and the rest are counted.
MAX_RENDERED_GROUPS = 200


class _ScanHandle:
    """What the main window's close guard needs to stop a running scan."""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self._done = threading.Event()
        self._done.set()

    @property
    def busy(self) -> bool:
        return not self._done.is_set()

    def begin(self) -> None:
        self.stop.clear()
        self._done.clear()

    def finish(self) -> None:
        self._done.set()

    def cancel(self) -> bool:
        if not self.busy:
            return False
        self.stop.set()
        return True

    def wait_for_done(self, timeout_ms: int = -1) -> bool:
        return self._done.wait(None if timeout_ms < 0 else timeout_ms / 1000)


class SimilarReviewPage(BasePage):
    progress_changed = Signal(int, int, str)

    def __init__(
        self,
        paths: RuntimePaths | None = None,
        *,
        service: SimilarPhotoService | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            "Similar Photos",
            "Find resized, recompressed and re-exported copies that exact-duplicate scans cannot see. "
            "Nothing is moved unless you choose it.",
            parent,
        )
        self.paths = paths or get_runtime_paths()
        self.service = service or SimilarPhotoService(self.paths)
        self.controller = _ScanHandle()
        self.review: SimilarPhotoReview | None = None
        self.checks: dict[str, QCheckBox] = {}
        self.group_keepers: dict[str, str] = {}
        self.recommended_keepers: dict[str, str] = {}
        self.item_sizes: dict[str, int] = {}
        self.item_group: dict[str, str] = {}
        self.cards: list[Card] = []
        self.group_cards: dict[str, Card] = {}
        self.tile_widgets: dict[str, dict[str, Any]] = {}

        self.progress_changed.connect(self._on_progress)

        self.banner = ToastBanner("", "info")
        self.banner.hide()
        self.content.addWidget(self.banner)

        setup = Card()
        setup_layout = QVBoxLayout(setup)
        setup_layout.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.XL)
        setup_layout.setSpacing(Spacing.LG)
        setup_layout.addWidget(
            SectionHeader("Choose photos to compare", "Subfolders are included. Photos are only read.")
        )
        self.path = PathSelector("Photo folder", "Choose a folder or drive", "")
        self.path.browse_requested.connect(self._browse)
        setup_layout.addWidget(self.path)

        options = QHBoxLayout()
        sensitivity_label = QLabel("Match")
        sensitivity_label.setProperty("role", "section")
        self.sensitivity = QComboBox()
        self.sensitivity.addItem("Strict — near-identical only", "strict")
        self.sensitivity.addItem("Balanced — resized or recompressed copies", "balanced")
        self.sensitivity.addItem("Loose — also light edits and crops", "loose")
        self.sensitivity.setCurrentIndex(self.sensitivity.findData(DEFAULT_SENSITIVITY))
        set_accessible(self.sensitivity, "Match sensitivity")
        self.scan_button = PrimaryButton("Find similar photos")
        set_accessible(self.scan_button, "Find similar photos", "Read the folder and group photos that look alike.")
        self.scan_button.clicked.connect(self.start_scan)
        options.addWidget(sensitivity_label)
        options.addWidget(self.sensitivity, 1)
        options.addWidget(self.scan_button)
        setup_layout.addLayout(options)
        self.content.addWidget(setup)

        self.progress_panel = ProgressPanel()
        self.progress_panel.cancel_requested.connect(self.controller.cancel)
        self.progress_panel.hide()
        self.content.addWidget(self.progress_panel)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.hide()
        self.content.addWidget(self.summary)

        # Bulk review controls: filter and bulk selection actions
        self.controls_row = QWidget()
        controls_layout = QHBoxLayout(self.controls_row)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(Spacing.MD)

        show_label = QLabel("Show")
        show_label.setProperty("role", "section")
        self.show_combo = QComboBox()
        self.show_combo.addItem("All groups", "all")
        self.show_combo.addItem("Identical copies only", "identical")
        self.show_combo.addItem("Similar only", "similar")
        set_accessible(self.show_combo, "Show", "Filter groups by type")
        self.show_combo.currentIndexChanged.connect(lambda _idx: self._apply_filter())
        self.filter_combo = self.show_combo

        self.select_all_button = QPushButton("Select all except recommended")
        set_accessible(
            self.select_all_button,
            "Select all except recommended",
            "Tick every non-keeper photo in every rendered group",
        )
        self.select_all_button.clicked.connect(self.select_all_except_recommended)

        self.clear_selection_button = QPushButton("Clear selection")
        set_accessible(self.clear_selection_button, "Clear selection", "Deselect all photos")
        self.clear_selection_button.clicked.connect(self.clear_selection)

        controls_layout.addWidget(show_label)
        controls_layout.addWidget(self.show_combo)
        controls_layout.addWidget(self.select_all_button)
        controls_layout.addWidget(self.clear_selection_button)
        controls_layout.addStretch(1)
        self.controls_row.hide()
        self.content.addWidget(self.controls_row)

        self.results = QVBoxLayout()
        self.results.setSpacing(Spacing.LG)
        self.content.addLayout(self.results)

        self.filter_empty = QLabel("No groups match the current filter.")
        self.filter_empty.setProperty("muted", True)
        self.filter_empty.hide()
        self.content.addWidget(self.filter_empty)

        actions = QHBoxLayout()
        self.selection_label = QLabel("No photos selected.")
        self.selection_label.setProperty("muted", True)
        self.quarantine_button = PrimaryButton("Move selected to quarantine")
        self.quarantine_button.setEnabled(False)
        set_accessible(
            self.quarantine_button,
            "Move selected to quarantine",
            "Selected photos move into the app's quarantine and can be restored.",
        )
        self.quarantine_button.clicked.connect(self.quarantine_selected)
        actions.addWidget(self.selection_label, 1)
        actions.addWidget(self.quarantine_button)
        self.actions_row = QWidget()
        self.actions_row.setLayout(actions)
        self.actions_row.hide()
        self.content.addWidget(self.actions_row)
        self.finish()

    # ------------------------------------------------------------- scanning

    def _browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose a photo folder")
        if selected:
            self.path.set_path(selected)

    def start_scan(self) -> bool:
        root = self.path.path()
        if not root or not Path(root).is_dir():
            self.path.set_error("Choose a folder that exists.")
            return False
        if self.controller.busy:
            return False
        self.path.set_error("")
        self.banner.hide()
        self.scan_button.setEnabled(False)
        self.progress_panel.show()
        self.progress_panel.update_progress(0, "Checking for cloud-only files…", "Reading file attributes only", "")
        self.start_after_cloud_check([root], False, lambda: self._run_scan(root), self._abandon_scan)
        return True

    def _abandon_scan(self) -> None:
        self.progress_panel.hide()
        self.scan_button.setEnabled(True)
        self._show_banner("Nothing was read or downloaded.", "info")

    def _run_scan(self, root: str) -> None:
        sensitivity = self.sensitivity.currentData() or DEFAULT_SENSITIVITY
        handle = self.controller
        handle.begin()
        self.progress_panel.update_progress(0, "Finding photos…", root, "")

        def work():
            try:
                return self.service.scan(
                    [root],
                    sensitivity=sensitivity,
                    stop_event=handle.stop,
                    progress=lambda done, total, message: self.progress_changed.emit(done, total, message),
                )
            finally:
                handle.finish()

        self.run_in_background(work, self._show_review, self._scan_failed)

    def _on_progress(self, done: int, total: int, message: str) -> None:
        value = int(done * 100 / total) if total else 0
        self.progress_panel.update_progress(value, message, "", f"{done:,} / {total:,}" if total else "")

    def _scan_failed(self, failure) -> None:
        self.progress_panel.hide()
        self.scan_button.setEnabled(True)
        message = getattr(failure, "message", "") or str(failure)
        self._show_banner(f"The comparison stopped: {message}", "error")

    # ------------------------------------------------------------- results

    def _clear_results(self) -> None:
        self.checks.clear()
        self.group_keepers.clear()
        self.recommended_keepers.clear()
        self.item_sizes.clear()
        self.item_group.clear()
        self.cards.clear()
        self.group_cards.clear()
        self.tile_widgets.clear()
        self.filter_empty.hide()
        while self.results.count():
            item = self.results.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _show_review(self, review: SimilarPhotoReview | None) -> None:
        self.progress_panel.hide()
        self.scan_button.setEnabled(True)
        self._clear_results()
        if review is None:
            self.review = None
            self.controls_row.hide()
            self.actions_row.hide()
            return
        if review.cancelled:
            self.review = review
            self.controls_row.hide()
            self.actions_row.hide()
            self._show_banner("Comparison cancelled. Nothing was changed.", "info")
            return
        if not review.groups:
            self.review = review
            self.controls_row.hide()
            self.actions_row.hide()
            self.results.addWidget(
                EmptyState("No similar photos", f"Compared {review.scanned_images:,} photos and found no lookalikes.")
            )
            return

        # Sort groups by recoverable bytes (sum of non-keeper sizes), largest first
        def _group_recoverable_bytes(g: SimilarPhotoGroup) -> int:
            return sum(item.size for item in g.items if item.id != g.keeper_id)

        sorted_groups = sorted(
            review.groups,
            key=_group_recoverable_bytes,
            reverse=True,
        )

        self.review = dataclasses.replace(
            review,
            groups=tuple(sorted_groups),
            duplicate_review=DuplicateReview(
                groups=tuple(g.group for g in sorted_groups),
                scanned_files=review.scanned_images,
            ),
        )

        for group in self.review.groups:
            self.group_keepers[group.id] = group.keeper_id
            self.recommended_keepers[group.id] = group.keeper_id
            for item in group.items:
                self.item_sizes[item.id] = item.size
                self.item_group[item.id] = group.id

        self.summary.setText(
            f"{len(self.review.groups):,} groups • {self.review.photo_count:,} photos • compared {self.review.scanned_images:,}. "
            "The recommended keep is marked in each group. Nothing is selected until you select it."
        )
        self.summary.show()

        for group in self.review.groups[:MAX_RENDERED_GROUPS]:
            card = self._group_card(group)
            self.cards.append(card)
            self.group_cards[group.id] = card
            self.results.addWidget(card)

        hidden = len(self.review.groups) - MAX_RENDERED_GROUPS
        if hidden > 0:
            more = QLabel(f"{hidden:,} more groups are not shown. Narrow the folder to review them.")
            more.setProperty("muted", True)
            self.results.addWidget(more)

        if self.review.errors:
            self._show_banner(f"{len(self.review.errors):,} photos could not be read and were skipped.", "warning")

        self.controls_row.show()
        self.show_combo.blockSignals(True)
        self.show_combo.setCurrentIndex(0)
        self.show_combo.blockSignals(False)
        self._apply_filter()

        self.actions_row.show()
        self._update_selection()

    def _apply_filter(self) -> None:
        selected_filter = self.show_combo.currentData() or "all"
        visible_count = 0
        for card in self.cards:
            kind = getattr(card, "kind", "all")
            matches = (selected_filter == "all") or (kind == selected_filter)
            card.matches_filter = matches
            card.setVisible(matches)
            if matches:
                visible_count += 1
        if self.cards and visible_count == 0:
            self.filter_empty.show()
        else:
            self.filter_empty.hide()

    def select_all_except_recommended(self) -> None:
        """Tick every non-keeper photo in every rendered group."""
        for group_id, card in self.group_cards.items():
            if not getattr(card, "matches_filter", True) or card.isHidden():
                continue
            keeper_id = self.group_keepers.get(group_id)
            for item_id, widgets in self.tile_widgets.items():
                if self.item_group.get(item_id) == group_id:
                    check = widgets["check"]
                    if item_id != keeper_id and check.isEnabled():
                        check.blockSignals(True)
                        check.setChecked(True)
                        check.blockSignals(False)
        self._update_selection()

    def clear_selection(self) -> None:
        """Deselect all selectable photos."""
        for check in self.checks.values():
            if check.isEnabled() and check.isChecked():
                check.blockSignals(True)
                check.setChecked(False)
                check.blockSignals(False)
        self._update_selection()

    def _group_card(self, group: SimilarPhotoGroup) -> Card:
        card = Card()
        card.group_id = group.id
        card.kind = group.kind
        card.matches_filter = True
        layout = QVBoxLayout(card)
        layout.setContentsMargins(Spacing.LG, Spacing.LG, Spacing.LG, Spacing.LG)
        title = "Identical copies" if group.kind == "identical" else "Similar photos"
        heading = QLabel(f"{title} • {len(group.items)} photos")
        heading.setProperty("role", "section")
        layout.addWidget(heading)
        if group.reasons:
            reason = QLabel("Recommended keep: " + "; ".join(group.reasons))
            reason.setWordWrap(True)
            reason.setProperty("muted", True)
            layout.addWidget(reason)
        row = QHBoxLayout()
        row.setSpacing(Spacing.MD)
        for item in group.items:
            row.addWidget(self._tile(group, item, item.id == group.keeper_id))
        row.addStretch(1)
        layout.addLayout(row)
        return card

    def _tile(self, group: SimilarPhotoGroup, item, is_keeper: bool) -> QFrame:
        tile = QFrame()
        tile.setProperty("subtleCard", True)
        tile.setMaximumWidth(220)
        layout = QVBoxLayout(tile)
        layout.setContentsMargins(Spacing.SM, Spacing.SM, Spacing.SM, Spacing.SM)
        preview = QLabel()
        preview.setFixedSize(96, 96)
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap(item.thumbnail_path) if item.thumbnail_path else QPixmap()
        if pixmap.isNull():
            preview.setText(item.preview_status or "No preview")
            preview.setWordWrap(True)
        else:
            preview.setPixmap(pixmap)
        layout.addWidget(preview)
        name = QLabel(item.filename)
        name.setWordWrap(True)
        name.setToolTip(item.path)
        layout.addWidget(name)
        details = QLabel(
            " • ".join(part for part in (item.dimensions, format_duplicate_size(item.size), self._date(item.modified)) if part)
        )
        details.setProperty("role", "caption")
        details.setWordWrap(True)
        layout.addWidget(details)

        badge = QLabel("Recommended keep" if is_keeper else "Keeper")
        badge.setProperty("status", "success")
        if not is_keeper:
            badge.hide()
        layout.addWidget(badge)

        keep_button = QPushButton("Keep this one instead")
        set_accessible(keep_button, f"Keep {item.filename} instead")
        keep_button.clicked.connect(lambda _checked=False, i_id=item.id, g_id=group.id: self._set_group_keeper(g_id, i_id))
        if is_keeper:
            keep_button.hide()
        layout.addWidget(keep_button)

        check = QCheckBox("Move to quarantine")
        set_accessible(check, f"Move {item.filename} to quarantine")
        check.toggled.connect(lambda _checked: self._update_selection())
        if is_keeper:
            check.setEnabled(False)
            check.setChecked(False)
        else:
            check.setEnabled(True)
            check.setChecked(False)
        self.checks[item.id] = check
        layout.addWidget(check)

        self.tile_widgets[item.id] = {
            "badge": badge,
            "keep_button": keep_button,
            "check": check,
            "tile": tile,
        }
        return tile

    def _set_group_keeper(self, group_id: str, new_keeper_id: str) -> None:
        """User choice overrides the recommendation: move keeper mark to this tile."""
        old_keeper_id = self.group_keepers.get(group_id)
        if not old_keeper_id or old_keeper_id == new_keeper_id:
            return

        old_widgets = self.tile_widgets.get(old_keeper_id)
        new_widgets = self.tile_widgets.get(new_keeper_id)
        if not old_widgets or not new_widgets:
            return

        old_check = old_widgets["check"]
        new_check = new_widgets["check"]

        # Swap which checkbox is disabled and what is selected
        was_new_checked = new_check.isChecked()

        # Update new keeper
        recommended_id = self.recommended_keepers.get(group_id)
        new_badge_text = "Recommended keep" if new_keeper_id == recommended_id else "Keeper"
        new_widgets["badge"].setText(new_badge_text)
        new_widgets["badge"].show()
        new_widgets["keep_button"].hide()
        new_check.blockSignals(True)
        new_check.setChecked(False)
        new_check.setEnabled(False)
        new_check.blockSignals(False)

        # Update old keeper (becomes selectable)
        old_widgets["badge"].hide()
        old_widgets["keep_button"].show()
        old_check.blockSignals(True)
        old_check.setEnabled(True)
        old_check.setChecked(was_new_checked)
        old_check.blockSignals(False)

        # Update keeper tracking
        self.group_keepers[group_id] = new_keeper_id

        # Update review data structure
        if self.review is not None:
            self._update_review_keeper(group_id, new_keeper_id)

        self._update_selection()

    def _update_review_keeper(self, group_id: str, new_keeper_id: str) -> None:
        if self.review is None:
            return
        new_groups = []
        for g in self.review.groups:
            if g.id == group_id:
                new_duplicate_group = DuplicateGroup(
                    id=g.group.id,
                    hash=g.group.hash,
                    items=g.group.items,
                    keep_item_id=new_keeper_id,
                    selected_item_ids=g.group.selected_item_ids,
                )
                new_groups.append(
                    SimilarPhotoGroup(
                        group=new_duplicate_group,
                        kind=g.kind,
                        keeper_id=new_keeper_id,
                        reasons=g.reasons,
                    )
                )
            else:
                new_groups.append(g)
        self.review = dataclasses.replace(
            self.review,
            groups=tuple(new_groups),
            duplicate_review=DuplicateReview(
                groups=tuple(g.group for g in new_groups),
                scanned_files=self.review.scanned_images,
            ),
        )

    @staticmethod
    def _date(timestamp: float) -> str:
        try:
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            return ""

    def selected_ids(self) -> list[str]:
        return [item_id for item_id, check in self.checks.items() if check.isChecked()]

    def _update_selection(self) -> None:
        selected = self.selected_ids()
        count = len(selected)
        if count:
            total_size = sum(self.item_sizes.get(item_id, 0) for item_id in selected)
            size_str = format_duplicate_size(total_size)
            self.selection_label.setText(f"{count:,} photo(s) selected ({size_str}).")
        else:
            self.selection_label.setText("No photos selected.")
        self.quarantine_button.setEnabled(bool(count) and not self.controller.busy)

    # ------------------------------------------------------------- quarantine

    def quarantine_selected(self) -> None:
        selected = self.selected_ids()
        if not selected or self.review is None:
            return
        total_size = sum(self.item_sizes.get(item_id, 0) for item_id in selected)
        size_str = format_duplicate_size(total_size)
        dialog = ConfirmationDialog(
            "Move photos to quarantine?",
            f"{len(selected):,} photo(s) ({size_str}) will move into the app's quarantine. Nothing is deleted, "
            "and every photo can be restored from the Quarantine page.",
            "Move to quarantine",
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        review = self.review
        self.quarantine_button.setEnabled(False)
        self.run_in_background(
            lambda: self.service.quarantine(review, selected),
            lambda result: self._quarantined(result, selected),
            self._quarantine_failed,
        )

    def _quarantined(self, result, selected: list[str]) -> None:
        moved = len(getattr(result, "records", ()) or ())
        failures = len(getattr(result, "failures", ()) or ())
        for item_id in selected:
            widgets = self.tile_widgets.get(item_id)
            if widgets is not None:
                check = widgets["check"]
                check.setChecked(False)
                check.setEnabled(False)
                check.setText("In quarantine")
                widgets["keep_button"].setEnabled(False)
                widgets["keep_button"].hide()
            else:
                check = self.checks.get(item_id)
                if check is not None:
                    check.setChecked(False)
                    check.setEnabled(False)
                    check.setText("In quarantine")
        text = f"Moved {moved:,} photo(s) to quarantine. Restore them from the Quarantine page."
        if failures:
            text += f" {failures:,} could not be moved and were left in place."
        self._show_banner(text, "warning" if failures else "success")
        self._update_selection()

    def _quarantine_failed(self, failure) -> None:
        message = getattr(failure, "message", "") or str(failure)
        self._show_banner(message, "error")
        self._update_selection()

    def _show_banner(self, message: str, kind: str) -> None:
        self.banner.set_message(message, kind)
        self.banner.show()
