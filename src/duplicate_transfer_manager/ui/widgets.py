"""Reusable accessible controls shared by every application page."""

from __future__ import annotations

from collections.abc import Iterable
import math

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QProgressBar,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .icons import icon, icon_size
from .theme import Spacing


def format_eta(seconds: float | int | None) -> str:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(value) or value < 0:
        return "—"
    value = int(value)
    if value < 60:
        return f"{value}s"
    minutes, sec = divmod(value, 60)
    if minutes < 60:
        return f"{minutes}m {sec}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def set_accessible(widget: QWidget, name: str, description: str = "") -> QWidget:
    widget.setAccessibleName(name)
    if description:
        widget.setAccessibleDescription(description)
        widget.setToolTip(description)
    return widget


class Card(QFrame):
    def __init__(self, parent: QWidget | None = None, *, subtle: bool = False) -> None:
        super().__init__(parent)
        self.setProperty("subtleCard" if subtle else "card", True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)


class PageHeader(QWidget):
    def __init__(self, title: str, subtitle: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, Spacing.SM)
        layout.setSpacing(Spacing.XS)
        self.title_label = QLabel(title)
        self.title_label.setProperty("role", "title")
        self.title_label.setAccessibleName(f"{title} page")
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setProperty("role", "subtitle")
        self.subtitle_label.setWordWrap(True)
        layout.addWidget(self.title_label)
        layout.addWidget(self.subtitle_label)


class SectionHeader(QWidget):
    action_requested = Signal()

    def __init__(
        self,
        title: str,
        subtitle: str = "",
        action_text: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        title_label = QLabel(title)
        title_label.setProperty("role", "section")
        text_layout.addWidget(title_label)
        if subtitle:
            subtitle_label = QLabel(subtitle)
            subtitle_label.setProperty("muted", True)
            subtitle_label.setWordWrap(True)
            text_layout.addWidget(subtitle_label)
        layout.addLayout(text_layout, 1)
        if action_text:
            action = QPushButton(action_text)
            action.setProperty("variant", "quiet")
            set_accessible(action, action_text)
            action.clicked.connect(self.action_requested)
            layout.addWidget(action)


class PrimaryButton(QPushButton):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setProperty("variant", "primary")
        set_accessible(self, text)


class SourceCard(QToolButton):
    def __init__(
        self,
        title: str,
        description: str,
        icon_name: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("sourceCard", True)
        self.setCheckable(True)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.setText(f"{title}\n{description}")
        self.setIcon(icon(icon_name))
        self.setIconSize(icon_size(28))
        set_accessible(self, title, description)


class SourcePicker(QWidget):
    selection_changed = Signal(str)

    def __init__(
        self,
        choices: Iterable[tuple[str, str, str, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.MD)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.cards: dict[str, SourceCard] = {}
        for index, (key, title, description, icon_name) in enumerate(choices):
            card = SourceCard(title, description, icon_name)
            self.group.addButton(card, index)
            self.cards[key] = card
            layout.addWidget(card, 1)
            card.clicked.connect(
                lambda checked=False, selected=key: checked
                and self.selection_changed.emit(selected)
            )
        layout.addStretch()

    def select(self, key: str) -> None:
        card = self.cards.get(key)
        if card:
            card.setChecked(True)
            self.selection_changed.emit(key)

    def selected_key(self) -> str:
        checked = self.group.checkedButton()
        return next(
            (key for key, card in self.cards.items() if card is checked),
            "",
        )


class PathSelector(QWidget):
    browse_requested = Signal()
    path_changed = Signal(str)

    def __init__(
        self,
        label: str,
        placeholder: str,
        helper_text: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.XS)
        label_widget = QLabel(label)
        label_widget.setProperty("role", "section")
        layout.addWidget(label_widget)
        row = QHBoxLayout()
        row.setSpacing(Spacing.SM)
        self.entry = QLineEdit()
        self.entry.setPlaceholderText(placeholder)
        set_accessible(self.entry, label, helper_text or placeholder)
        self.entry.textChanged.connect(self.path_changed)
        self.browse_button = QPushButton("Browse")
        self.browse_button.setIcon(icon("folder"))
        self.browse_button.setIconSize(icon_size())
        set_accessible(self.browse_button, f"Browse for {label.lower()}")
        self.browse_button.clicked.connect(self.browse_requested)
        row.addWidget(self.entry, 1)
        row.addWidget(self.browse_button)
        layout.addLayout(row)
        self.helper = QLabel(helper_text)
        self.helper.setProperty("role", "caption")
        self.helper.setWordWrap(True)
        self.helper.setVisible(bool(helper_text))
        layout.addWidget(self.helper)
        self.validation = QLabel()
        self.validation.setProperty("status", "danger")
        self.validation.setWordWrap(True)
        self.validation.setVisible(False)
        layout.addWidget(self.validation)

    def path(self) -> str:
        return self.entry.text().strip()

    def set_path(self, path: str) -> None:
        self.entry.setText(path)

    def set_error(self, message: str = "") -> None:
        self.validation.setText(message)
        self.validation.setVisible(bool(message))
        self.entry.setProperty("invalid", bool(message))
        self.entry.style().unpolish(self.entry)
        self.entry.style().polish(self.entry)


class InlineMessage(QFrame):
    def __init__(
        self,
        message: str,
        kind: str = "info",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("banner", kind)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        marker = QLabel("●")
        marker.setProperty("status", "danger" if kind == "danger" else kind)
        self.label = QLabel(message)
        self.label.setWordWrap(True)
        layout.addWidget(marker)
        layout.addWidget(self.label, 1)
        set_accessible(self, f"{kind.title()} message", message)

    def set_message(self, message: str, kind: str | None = None) -> None:
        self.label.setText(message)
        if kind:
            self.setProperty("banner", kind)
            self.style().unpolish(self)
            self.style().polish(self)


class ToastBanner(InlineMessage):
    dismissed = Signal()

    def __init__(
        self,
        message: str,
        kind: str = "info",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(message, kind, parent)
        close = QPushButton("Dismiss")
        close.setProperty("variant", "quiet")
        close.clicked.connect(self._dismiss)
        self.layout().addWidget(close)

    def _dismiss(self) -> None:
        self.hide()
        self.dismissed.emit()


class ProgressPanel(Card):
    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(Spacing.LG, Spacing.LG, Spacing.LG, Spacing.LG)
        top = QHBoxLayout()
        self.title = QLabel("Preparing…")
        self.title.setProperty("role", "section")
        self.detail = QLabel("Waiting to begin")
        self.detail.setProperty("muted", True)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_requested)
        top.addWidget(self.title)
        top.addStretch()
        top.addWidget(self.cancel_button)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.metrics = QLabel("0%  •  0 items  •  ETA —")
        self.metrics.setProperty("role", "caption")
        layout.addLayout(top)
        layout.addWidget(self.detail)
        layout.addWidget(self.progress)
        layout.addWidget(self.metrics)
        set_accessible(self.progress, "Operation progress")

    def update_progress(
        self,
        value: int,
        title: str,
        detail: str,
        metrics: str,
    ) -> None:
        self.progress.setValue(max(0, min(100, value)))
        self.title.setText(title)
        self.detail.setText(detail)
        self.metrics.setText(metrics)


class EmptyState(Card):
    action_requested = Signal()

    def __init__(
        self,
        title: str,
        description: str,
        action_text: str = "",
        icon_name: str = "info",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(Spacing.XXL, Spacing.XXL, Spacing.XXL, Spacing.XXL)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image = QLabel()
        image.setPixmap(icon(icon_name).pixmap(40, 40))
        image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label = QLabel(title)
        title_label.setProperty("role", "section")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        description_label = QLabel(description)
        description_label.setProperty("muted", True)
        description_label.setWordWrap(True)
        description_label.setMaximumWidth(520)
        description_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(image)
        layout.addWidget(title_label)
        layout.addWidget(description_label)
        if action_text:
            action = PrimaryButton(action_text)
            action.clicked.connect(self.action_requested)
            layout.addWidget(action, alignment=Qt.AlignmentFlag.AlignCenter)
        set_accessible(self, title, description)


class MetricCard(Card):
    def __init__(
        self,
        value: str,
        label: str,
        detail: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(Spacing.LG, Spacing.LG, Spacing.LG, Spacing.LG)
        self.value_label = QLabel(value)
        self.value_label.setProperty("role", "title")
        self.label_widget = QLabel(label)
        self.label_widget.setProperty("role", "section")
        layout.addWidget(self.value_label)
        layout.addWidget(self.label_widget)
        if detail:
            detail_widget = QLabel(detail)
            detail_widget.setProperty("muted", True)
            detail_widget.setWordWrap(True)
            layout.addWidget(detail_widget)


class CompletionSummary(Card):
    def __init__(
        self,
        title: str = "Operation complete",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.XL)
        heading = QLabel(title)
        heading.setProperty("role", "title")
        heading.setProperty("status", "success")
        self.metrics = QGridLayout()
        layout.addWidget(heading)
        layout.addLayout(self.metrics)

    def set_metrics(self, values: Iterable[tuple[str, str]]) -> None:
        while self.metrics.count():
            item = self.metrics.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for index, (label, value) in enumerate(values):
            card = MetricCard(value, label, parent=self)
            self.metrics.addWidget(card, index // 3, index % 3)


class ConfirmationDialog(QDialog):
    def __init__(
        self,
        title: str,
        message: str,
        confirm_text: str = "Continue",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(Spacing.XL, Spacing.XL, Spacing.XL, Spacing.XL)
        heading = QLabel(title)
        heading.setProperty("role", "title")
        body = QLabel(message)
        body.setWordWrap(True)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(confirm_text)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setProperty("variant", "primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(heading)
        layout.addWidget(body)
        layout.addWidget(buttons)


class StepIndicator(QWidget):
    def __init__(self, steps: Iterable[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.steps = list(steps)
        self.buttons: list[QToolButton] = []
        self.labels: list[QLabel] = []
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.SM)
        for index, name in enumerate(self.steps):
            button = QToolButton()
            button.setText(str(index + 1))
            button.setProperty("step", True)
            button.setProperty("stepState", "active" if index == 0 else "pending")
            button.setEnabled(False)
            label = QLabel(name)
            label.setProperty("muted", index != 0)
            layout.addWidget(button)
            layout.addWidget(label)
            self.buttons.append(button)
            self.labels.append(label)
            if index < len(self.steps) - 1:
                separator = QFrame()
                separator.setFrameShape(QFrame.Shape.HLine)
                separator.setMinimumWidth(16)
                layout.addWidget(separator, 1)

    def set_current(self, index: int) -> None:
        """Show completed, active, and pending workflow stages."""
        if not self.buttons:
            return
        current = max(0, min(index, len(self.buttons) - 1))
        for position, (button, label) in enumerate(zip(self.buttons, self.labels)):
            state = "complete" if position < current else "active" if position == current else "pending"
            button.setProperty("stepState", state)
            label.setProperty("muted", position != current)
            button.style().unpolish(button)
            button.style().polish(button)
            label.style().unpolish(label)
            label.style().polish(label)


class DisclosurePanel(QWidget):
    def __init__(
        self,
        title: str = "Advanced options",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.SM)
        self.toggle = QPushButton(f"›  {title}")
        self.toggle.setCheckable(True)
        self.toggle.setProperty("variant", "quiet")
        self.toggle.setAccessibleName(title)
        self.toggle.clicked.connect(self.set_expanded)
        self.body = Card(subtle=True)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(
            Spacing.LG,
            Spacing.LG,
            Spacing.LG,
            Spacing.LG,
        )
        self.body_layout.setSpacing(Spacing.MD)
        self.body.hide()
        layout.addWidget(self.toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.body)

    def set_expanded(self, expanded: bool) -> None:
        self.toggle.blockSignals(True)
        self.toggle.setChecked(expanded)
        self.toggle.blockSignals(False)
        self.toggle.setText(
            ("⌄  " if expanded else "›  ") + self.toggle.text()[3:]
        )
        self.body.setVisible(expanded)


class ResponsiveGrid(QWidget):
    """Reflow cards from columns to one column at narrow widths."""

    def __init__(
        self,
        widgets: Iterable[QWidget],
        *,
        min_column_width: int = 300,
        max_columns: int = 3,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.widgets = list(widgets)
        self.min_column_width = min_column_width
        self.max_columns = max_columns
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(Spacing.MD)
        self._columns = 0
        self._reflow(1)

    def resizeEvent(self, event) -> None:
        width = max(1, event.size().width())
        columns = max(1, min(self.max_columns, width // self.min_column_width))
        self._reflow(columns)
        super().resizeEvent(event)

    def minimumSizeHint(self) -> QSize:
        """Permit the parent to shrink the grid far enough to trigger reflow."""
        if not self.widgets:
            return QSize(0, 0)
        width = max(widget.minimumSizeHint().width() for widget in self.widgets)
        height = max(widget.minimumSizeHint().height() for widget in self.widgets)
        return QSize(width, height)

    def _reflow(self, columns: int) -> None:
        if columns == self._columns:
            return
        self._columns = columns
        while self.grid.count():
            self.grid.takeAt(0)
        for index, widget in enumerate(self.widgets):
            self.grid.addWidget(widget, index // columns, index % columns)
        for column in range(columns):
            self.grid.setColumnStretch(column, 1)


def add_shortcut(button: QAbstractButton, sequence: str) -> QShortcut:
    shortcut = QShortcut(QKeySequence(sequence), button)
    shortcut.activated.connect(button.click)
    return shortcut
