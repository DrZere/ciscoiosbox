"""Interface grid with administrative controls."""
from __future__ import annotations

import logging

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal,
)
from PySide6.QtGui import QAction, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMenu, QMessageBox, QPushButton, QTableView, QVBoxLayout, QWidget,
)

from ..core.models import InterfaceRow
from ..services.interface_service import InterfaceService
from .theme import Palette, monospace_font

log = logging.getLogger(__name__)

#: Sort role carrying a comparable key, so "Gi1/0/2" sorts before "Gi1/0/10".
SORT_ROLE = Qt.ItemDataRole.UserRole + 1
ROW_ROLE = Qt.ItemDataRole.UserRole + 2


class InterfaceTableModel(QAbstractTableModel):
    """Table model over a list of :class:`InterfaceRow`."""

    COLUMNS = [
        ("Interface", "name"),
        ("Description", "description"),
        ("Status", "oper_status"),
        ("Admin", "admin_status"),
        ("VLAN", "vlan"),
        ("Mode", "mode"),
        ("Duplex", "duplex"),
        ("Speed", "speed"),
        ("IP Address", "ip_address"),
        ("Type", "media_type"),
    ]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[InterfaceRow] = []

    # ── data plumbing ─────────────────────────────────────────────────────────

    def set_rows(self, rows: list[InterfaceRow]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row_at(self, row: int) -> InterfaceRow | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def update_admin_state(self, name: str, shutdown: bool) -> None:
        """Patch one row in place after a successful toggle."""
        for index, row in enumerate(self._rows):
            if row.name == name:
                row.admin_status = "administratively down" if shutdown else "up"
                if shutdown:
                    row.oper_status = "disabled"
                self.dataChanged.emit(
                    self.index(index, 0),
                    self.index(index, len(self.COLUMNS) - 1))
                return

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - Qt override
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - Qt override
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section][0]
        return section + 1

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        column = index.column()
        _, attribute = self.COLUMNS[column]

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(row, column, attribute)

        if role == SORT_ROLE:
            if attribute == "name":
                from ..parsers.interfaces import natural_sort_key

                return natural_sort_key(row.name)
            if attribute == "speed":
                return self._speed_value(row.speed)
            return getattr(row, attribute, "")

        if role == ROW_ROLE:
            return row

        if role == Qt.ItemDataRole.ForegroundRole:
            return self._foreground(row, attribute)

        if role == Qt.ItemDataRole.FontRole:
            if attribute in ("name", "ip_address"):
                font = monospace_font(11)
                if attribute == "name":
                    font.setBold(True)
                return font

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if attribute in ("vlan", "duplex", "speed"):
                return int(Qt.AlignmentFlag.AlignCenter)

        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(row)

        return None

    # ── presentation ──────────────────────────────────────────────────────────

    @staticmethod
    def _display(row: InterfaceRow, column: int, attribute: str) -> str:
        if attribute == "name":
            return row.short_name
        if attribute == "admin_status":
            return "Shut" if row.is_shutdown else "Up"
        if attribute == "oper_status":
            return row.oper_status or "—"
        if attribute == "mode":
            return row.mode.title() if row.mode else "—"
        value = getattr(row, attribute, "")
        return str(value) if value else "—"

    @staticmethod
    def _foreground(row: InterfaceRow, attribute: str) -> QColor | None:
        if attribute == "oper_status":
            status = row.oper_status.lower()
            if status in ("up", "connected"):
                return QColor(Palette.SUCCESS)
            if "err" in status:                      # err-disabled
                return QColor(Palette.DANGER)
            if status in ("disabled", "admin down"):
                return QColor(Palette.TEXT_FAINT)
            return QColor(Palette.TEXT_MUTED)

        if attribute == "admin_status":
            return QColor(Palette.WARNING if row.is_shutdown else Palette.SUCCESS)

        if attribute == "mode" and row.mode == "trunk":
            return QColor(Palette.INFO)

        if attribute == "description" and not row.description:
            return QColor(Palette.TEXT_FAINT)

        # Dim every column of a shut port so the eye skips it.
        if row.is_shutdown:
            return QColor(Palette.TEXT_FAINT)
        return None

    @staticmethod
    def _speed_value(speed: str) -> int:
        """Numeric sort key for the speed column ('1000', 'a-1000', 'auto')."""
        digits = "".join(c for c in speed if c.isdigit())
        return int(digits) if digits else -1

    @staticmethod
    def _tooltip(row: InterfaceRow) -> str:
        lines = [f"<b>{row.name}</b>"]
        if row.description:
            lines.append(row.description)
        lines.append(f"Admin: {'shut down' if row.is_shutdown else 'enabled'}")
        lines.append(f"Line protocol: {row.oper_status or 'unknown'}")
        if row.ip_address:
            lines.append(f"IP: {row.ip_address}")
        if row.vlan:
            lines.append(f"VLAN: {row.vlan}")
        if row.media_type:
            lines.append(f"Media: {row.media_type}")
        return "<br>".join(lines)


class InterfacesView(QWidget):
    """Interface grid plus the actions that operate on the selection."""

    #: Request that the monitoring tab start graphing this interface.
    monitor_requested = Signal(str)
    #: Request that the VLAN tab open with these ports pre-selected.
    assign_vlan_requested = Signal(list)

    def __init__(self, service: InterfaceService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self._build_ui()
        self._wire_service()

    # ── construction ──────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(9)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setProperty("accent", True)
        self.refresh_button.clicked.connect(self.refresh)
        toolbar.addWidget(self.refresh_button)

        self.enable_button = QPushButton("No Shutdown")
        self.enable_button.setToolTip("Bring the selected interfaces up")
        self.enable_button.clicked.connect(lambda: self._set_admin_state(False))
        toolbar.addWidget(self.enable_button)

        self.shutdown_button = QPushButton("Shutdown")
        self.shutdown_button.setProperty("danger", True)
        self.shutdown_button.setToolTip("Administratively disable the selected interfaces")
        self.shutdown_button.clicked.connect(lambda: self._set_admin_state(True))
        toolbar.addWidget(self.shutdown_button)

        toolbar.addSpacing(12)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter interfaces…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.setMaximumWidth(240)
        self.filter_edit.textChanged.connect(self._on_filter_changed)
        toolbar.addWidget(self.filter_edit)

        self.hide_down_check = QCheckBox("Connected only")
        self.hide_down_check.setToolTip("Hide interfaces that are not passing traffic")
        self.hide_down_check.toggled.connect(self._on_filter_changed)
        toolbar.addWidget(self.hide_down_check)

        toolbar.addStretch(1)

        self.summary_label = QLabel()
        self.summary_label.setProperty("muted", True)
        toolbar.addWidget(self.summary_label)

        layout.addLayout(toolbar)

        self.model = InterfaceTableModel(self)
        self.proxy = InterfaceFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SORT_ROLE)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        # Be explicit: enabling sorting without choosing a column leaves Qt to
        # pick, and it picked descending.
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.selectionModel().selectionChanged.connect(self._update_buttons)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)   # Description
        header.setStretchLastSection(False)
        layout.addWidget(self.table, 1)

        self.status_label = QLabel("Not loaded — click Refresh.")
        self.status_label.setProperty("muted", True)
        layout.addWidget(self.status_label)

        self._update_buttons()

    def _wire_service(self) -> None:
        self.service.interfaces_loaded.connect(self._on_loaded)
        self.service.admin_state_changed.connect(self.model.update_admin_state)
        self.service.busy_changed.connect(self._on_busy)

    # ── service callbacks ─────────────────────────────────────────────────────

    def refresh(self) -> None:
        self.status_label.setText("Loading interfaces…")
        self.service.refresh()

    def _on_loaded(self, rows: list[InterfaceRow]) -> None:
        self.model.set_rows(rows)
        self._resize_columns()
        self._update_summary(rows)
        self.status_label.setText(f"{len(rows)} interfaces.")

    def _on_busy(self, busy: bool) -> None:
        self.refresh_button.setEnabled(not busy)
        self.refresh_button.setText("Refreshing…" if busy else "Refresh")

    def _resize_columns(self) -> None:
        self.table.resizeColumnsToContents()
        header = self.table.horizontalHeader()
        # Give Description the slack; everything else fits its content.
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in range(self.model.columnCount()):
            if column != 1:
                header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
                width = header.sectionSize(column)
                header.resizeSection(column, min(max(width, 70), 220))

    def _update_summary(self, rows: list[InterfaceRow]) -> None:
        up = sum(1 for r in rows if r.is_up)
        shut = sum(1 for r in rows if r.is_shutdown)
        down = len(rows) - up - shut
        self.summary_label.setText(
            f"<span style='color:{Palette.SUCCESS}'>● {up} up</span>   "
            f"<span style='color:{Palette.TEXT_MUTED}'>● {down} down</span>   "
            f"<span style='color:{Palette.WARNING}'>● {shut} shut</span>")
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)

    # ── selection helpers ─────────────────────────────────────────────────────

    def selected_rows(self) -> list[InterfaceRow]:
        rows: list[InterfaceRow] = []
        for index in self.table.selectionModel().selectedRows():
            row = index.data(ROW_ROLE)
            if row is not None:
                rows.append(row)
        return rows

    def selected_names(self) -> list[str]:
        return [row.name for row in self.selected_rows()]

    def _update_buttons(self) -> None:
        rows = self.selected_rows()
        has_selection = bool(rows)
        self.enable_button.setEnabled(has_selection and any(r.is_shutdown for r in rows))
        self.shutdown_button.setEnabled(
            has_selection and any(not r.is_shutdown for r in rows))

    # ── filtering ─────────────────────────────────────────────────────────────

    def _on_filter_changed(self) -> None:
        self.proxy.set_text_filter(self.filter_edit.text())
        self.proxy.set_connected_only(self.hide_down_check.isChecked())
        visible = self.proxy.rowCount()
        total = self.model.rowCount()
        self.status_label.setText(
            f"{visible} of {total} interfaces shown."
            if visible != total else f"{total} interfaces.")

    # ── actions ───────────────────────────────────────────────────────────────

    def _set_admin_state(self, shutdown: bool) -> None:
        rows = self.selected_rows()
        targets = [r for r in rows if r.is_shutdown != shutdown]
        if not targets:
            return

        action = "Shut down" if shutdown else "Enable"
        if shutdown and len(targets) > 1:
            # Bulk shutdown is the one action here that can black out a rack.
            confirm = QMessageBox(self)
            confirm.setIcon(QMessageBox.Icon.Warning)
            confirm.setWindowTitle("Confirm Shutdown")
            confirm.setText(f"Shut down {len(targets)} interfaces?")
            confirm.setInformativeText(
                "Traffic on these ports will stop immediately:\n"
                + ", ".join(r.short_name for r in targets[:12])
                + ("…" if len(targets) > 12 else ""))
            confirm.setStandardButtons(
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes)
            confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if confirm.exec() != QMessageBox.StandardButton.Yes:
                return

        for row in targets:
            self.service.set_admin_state(row.name, shutdown)
        self.status_label.setText(f"{action} requested for {len(targets)} interface(s)…")

    def _edit_description(self) -> None:
        rows = self.selected_rows()
        if len(rows) != 1:
            return
        row = rows[0]
        text, ok = QInputDialog.getText(
            self, "Set Description", f"Description for {row.short_name}:",
            QLineEdit.EchoMode.Normal, row.description)
        if ok:
            self.service.set_description(row.name, text)

    def _on_double_click(self, index: QModelIndex) -> None:
        row = index.data(ROW_ROLE)
        if row is not None:
            self.monitor_requested.emit(row.name)

    def _show_context_menu(self, position) -> None:
        rows = self.selected_rows()
        if not rows:
            return

        menu = QMenu(self)
        names = ", ".join(r.short_name for r in rows[:3]) + ("…" if len(rows) > 3 else "")

        if any(r.is_shutdown for r in rows):
            menu.addAction(f"Enable {names}", lambda: self._set_admin_state(False))
        if any(not r.is_shutdown for r in rows):
            shutdown_action = QAction(f"Shut down {names}", menu)
            shutdown_action.triggered.connect(lambda: self._set_admin_state(True))
            menu.addAction(shutdown_action)

        menu.addSeparator()
        if len(rows) == 1:
            menu.addAction("Set description…", self._edit_description)
            menu.addAction("Graph traffic",
                           lambda: self.monitor_requested.emit(rows[0].name))
        menu.addAction(f"Assign VLAN to {len(rows)} port(s)…",
                       lambda: self.assign_vlan_requested.emit(
                           [r.name for r in rows]))
        menu.addSeparator()
        menu.addAction("Copy interface names", self._copy_names)

        menu.exec(self.table.viewport().mapToGlobal(position))

    def _copy_names(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText("\n".join(self.selected_names()))


class InterfaceFilterProxy(QSortFilterProxyModel):
    """Free-text filter plus a "connected only" toggle."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._text = ""
        self._connected_only = False

    def set_text_filter(self, text: str) -> None:
        self._text = text.strip().lower()
        self.invalidateFilter()

    def set_connected_only(self, enabled: bool) -> None:
        self._connected_only = enabled
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent) -> bool:  # noqa: N802
        model: InterfaceTableModel = self.sourceModel()
        row = model.row_at(source_row)
        if row is None:
            return False

        if self._connected_only and not row.is_up:
            return False

        if not self._text:
            return True

        haystack = " ".join([
            row.name, row.short_name, row.description, row.vlan,
            row.ip_address, row.oper_status, row.mode, row.media_type,
        ]).lower()
        return self._text in haystack
