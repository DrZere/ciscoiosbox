"""VLAN management: list the VLAN database, create/delete, assign ports."""
from __future__ import annotations

import logging

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMenu, QMessageBox, QPushButton, QSpinBox, QSplitter,
    QTableView, QVBoxLayout, QWidget,
)

from ..core.models import InterfaceRow, Vlan
from ..parsers import vlans as parse_vlan
from ..services.vlan_service import VlanService
from .theme import Palette, monospace_font

log = logging.getLogger(__name__)

VLAN_ROLE = Qt.ItemDataRole.UserRole + 1


class VlanTableModel(QAbstractTableModel):
    """Table model over the VLAN database."""

    COLUMNS = ["VLAN", "Name", "Status", "Ports"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[Vlan] = []

    def set_rows(self, rows: list[Vlan]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row_at(self, row: int) -> Vlan | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    @property
    def vlans(self) -> list[Vlan]:
        return list(self._rows)

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        vlan = self._rows[index.row()]
        column = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return str(vlan.vlan_id)
            if column == 1:
                return vlan.name or "—"
            if column == 2:
                return vlan.status or "—"
            if not vlan.interfaces:
                return "— no ports —"
            from ..parsers.interfaces import InterfaceRow as _Row

            shortened = [_Row(name=n).short_name for n in vlan.interfaces]
            preview = ", ".join(shortened[:8])
            return preview + (f"  (+{len(shortened) - 8} more)" if len(shortened) > 8 else "")

        if role == VLAN_ROLE:
            return vlan

        if role == Qt.ItemDataRole.ForegroundRole:
            if vlan.is_default:
                return QColor(Palette.TEXT_FAINT)
            if column == 2 and "act" not in vlan.status.lower():
                return QColor(Palette.WARNING)
            if column == 3 and not vlan.interfaces:
                return QColor(Palette.TEXT_FAINT)
            return None

        if role == Qt.ItemDataRole.FontRole and column == 0:
            font = monospace_font(11)
            font.setBold(True)
            return font

        if role == Qt.ItemDataRole.TextAlignmentRole and column == 0:
            return int(Qt.AlignmentFlag.AlignCenter)

        if role == Qt.ItemDataRole.ToolTipRole:
            lines = [f"<b>VLAN {vlan.vlan_id}</b> — {vlan.name or 'unnamed'}",
                     f"Status: {vlan.status}"]
            if vlan.is_default:
                lines.append("<i>Reserved by IOS; cannot be deleted.</i>")
            if vlan.interfaces:
                lines.append(f"{len(vlan.interfaces)} port(s):")
                lines.append(", ".join(vlan.interfaces))
            return "<br>".join(lines)

        return None


class VlansView(QWidget):
    """VLAN database table alongside the port-assignment panel."""

    def __init__(self, service: VlanService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self._interfaces: list[InterfaceRow] = []
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

        self.create_button = QPushButton("Create VLAN…")
        self.create_button.clicked.connect(self._create_vlan)
        toolbar.addWidget(self.create_button)

        self.delete_button = QPushButton("Delete")
        self.delete_button.setProperty("danger", True)
        self.delete_button.clicked.connect(self._delete_vlan)
        toolbar.addWidget(self.delete_button)

        toolbar.addStretch(1)
        self.count_label = QLabel()
        self.count_label.setProperty("muted", True)
        toolbar.addWidget(self.count_label)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # — VLAN table —
        self.model = VlanTableModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self.table)

        splitter.addWidget(self._build_assignment_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        self.status_label = QLabel("Not loaded — click Refresh.")
        self.status_label.setProperty("muted", True)
        layout.addWidget(self.status_label)

        self._update_buttons()

    def _build_assignment_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 0, 0, 0)

        box = QGroupBox("Assign Ports")
        box_layout = QVBoxLayout(box)

        hint = QLabel("Select ports, choose a mode, then apply.")
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        box_layout.addWidget(hint)

        self.port_list = QListWidget()
        self.port_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.port_list.setFont(monospace_font(11))
        self.port_list.itemSelectionChanged.connect(self._update_buttons)
        box_layout.addWidget(self.port_list, 1)

        selection_row = QHBoxLayout()
        select_all = QPushButton("All")
        select_all.clicked.connect(self.port_list.selectAll)
        selection_row.addWidget(select_all)
        select_none = QPushButton("None")
        select_none.clicked.connect(self.port_list.clearSelection)
        selection_row.addWidget(select_none)
        selection_row.addStretch(1)
        box_layout.addLayout(selection_row)

        form = QFormLayout()

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Access", "access")
        self.mode_combo.addItem("Trunk (802.1Q)", "trunk")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Mode", self.mode_combo)

        self.access_vlan_combo = QComboBox()
        self.access_vlan_label = QLabel("Access VLAN")
        form.addRow(self.access_vlan_label, self.access_vlan_combo)

        self.voice_vlan_spin = QSpinBox()
        self.voice_vlan_spin.setRange(0, 4094)
        self.voice_vlan_spin.setSpecialValueText("None")
        self.voice_vlan_label = QLabel("Voice VLAN")
        form.addRow(self.voice_vlan_label, self.voice_vlan_spin)

        self.allowed_edit = QLineEdit()
        self.allowed_edit.setPlaceholderText("all, or e.g. 10,20,30-35")
        self.allowed_label = QLabel("Allowed VLANs")
        form.addRow(self.allowed_label, self.allowed_edit)

        self.native_spin = QSpinBox()
        self.native_spin.setRange(0, 4094)
        self.native_spin.setSpecialValueText("Default (1)")
        self.native_label = QLabel("Native VLAN")
        form.addRow(self.native_label, self.native_spin)

        box_layout.addLayout(form)

        self.apply_button = QPushButton("Apply to Selected Ports")
        self.apply_button.setProperty("accent", True)
        self.apply_button.clicked.connect(self._apply_assignment)
        box_layout.addWidget(self.apply_button)

        layout.addWidget(box)
        self._on_mode_changed()
        return panel

    def _wire_service(self) -> None:
        self.service.vlans_loaded.connect(self._on_loaded)
        self.service.changed.connect(self.refresh)
        self.service.busy_changed.connect(self._on_busy)

    # ── data ──────────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        self.status_label.setText("Loading VLANs…")
        self.service.refresh()

    def set_interfaces(self, interfaces: list[InterfaceRow]) -> None:
        """Populate the port picker. Called when the interface grid reloads."""
        self._interfaces = [
            row for row in interfaces
            # Only physical switchports can be assigned a VLAN; SVIs and
            # loopbacks in this list would just produce rejected commands.
            if not row.name.startswith(("Vlan", "Loopback", "Tunnel", "Null"))
        ]
        selected = {item.data(Qt.ItemDataRole.UserRole)
                    for item in self.port_list.selectedItems()}

        self.port_list.clear()
        for row in self._interfaces:
            label = f"{row.short_name:<12} {row.description[:24]}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, row.name)
            if row.mode == "trunk":
                item.setForeground(QColor(Palette.INFO))
            elif row.is_shutdown:
                item.setForeground(QColor(Palette.TEXT_FAINT))
            item.setToolTip(f"{row.name} — {row.mode or 'unknown mode'}, "
                            f"VLAN {row.vlan or '?'}")
            self.port_list.addItem(item)
            if row.name in selected:
                item.setSelected(True)

    def preselect_ports(self, names: list[str]) -> None:
        """Select the given ports, e.g. when arriving from the interface grid."""
        wanted = set(names)
        self.port_list.clearSelection()
        for index in range(self.port_list.count()):
            item = self.port_list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) in wanted:
                item.setSelected(True)
        self._update_buttons()

    def _on_loaded(self, vlans: list[Vlan]) -> None:
        self.model.set_rows(vlans)
        self.count_label.setText(f"{len(vlans)} VLANs")
        self.status_label.setText(f"{len(vlans)} VLANs in the database.")

        current = self.access_vlan_combo.currentData()
        self.access_vlan_combo.clear()
        for vlan in vlans:
            if vlan.is_default and vlan.vlan_id != 1:
                continue
            label = f"{vlan.vlan_id} — {vlan.name}" if vlan.name else str(vlan.vlan_id)
            self.access_vlan_combo.addItem(label, vlan.vlan_id)
        if current is not None:
            index = self.access_vlan_combo.findData(current)
            if index >= 0:
                self.access_vlan_combo.setCurrentIndex(index)
        self._update_buttons()

    def _on_busy(self, busy: bool) -> None:
        self.refresh_button.setEnabled(not busy)
        self.refresh_button.setText("Refreshing…" if busy else "Refresh")
        self.apply_button.setEnabled(not busy and self._can_apply())

    # ── selection state ───────────────────────────────────────────────────────

    def selected_vlan(self) -> Vlan | None:
        indexes = self.table.selectionModel().selectedRows()
        return indexes[0].data(VLAN_ROLE) if indexes else None

    def selected_ports(self) -> list[str]:
        return [item.data(Qt.ItemDataRole.UserRole)
                for item in self.port_list.selectedItems()]

    def _can_apply(self) -> bool:
        if not self.selected_ports():
            return False
        if self.mode_combo.currentData() == "access":
            return self.access_vlan_combo.currentData() is not None
        return True

    def _on_selection_changed(self) -> None:
        self._update_buttons()
        vlan = self.selected_vlan()
        if vlan is not None:
            index = self.access_vlan_combo.findData(vlan.vlan_id)
            if index >= 0:
                self.access_vlan_combo.setCurrentIndex(index)

    def _update_buttons(self) -> None:
        vlan = self.selected_vlan()
        self.delete_button.setEnabled(vlan is not None and not vlan.is_default)
        if vlan is not None and vlan.is_default:
            self.delete_button.setToolTip(
                f"VLAN {vlan.vlan_id} is reserved by IOS and cannot be deleted.")
        else:
            self.delete_button.setToolTip("Delete the selected VLAN")
        self.apply_button.setEnabled(self._can_apply())

        count = len(self.selected_ports())
        self.apply_button.setText(
            f"Apply to {count} Port{'s' if count != 1 else ''}" if count
            else "Apply to Selected Ports")

    def _on_mode_changed(self) -> None:
        is_access = self.mode_combo.currentData() == "access"
        for widget in (self.access_vlan_label, self.access_vlan_combo,
                       self.voice_vlan_label, self.voice_vlan_spin):
            widget.setVisible(is_access)
        for widget in (self.allowed_label, self.allowed_edit,
                       self.native_label, self.native_spin):
            widget.setVisible(not is_access)
        self._update_buttons()

    # ── actions ───────────────────────────────────────────────────────────────

    def _create_vlan(self) -> None:
        dialog = CreateVlanDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        vlan_ids, name = dialog.result_values()
        if not vlan_ids:
            return

        existing = {v.vlan_id for v in self.model.vlans}
        clashes = [v for v in vlan_ids if v in existing]
        if clashes:
            answer = QMessageBox.question(
                self, "VLAN Already Exists",
                f"VLAN {', '.join(str(v) for v in clashes[:8])} already exist(s). "
                f"Applying will rename them. Continue?",
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
                QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Yes:
                return

        if len(vlan_ids) == 1:
            self.service.create_vlan(vlan_ids[0], name)
        else:
            self.service.create_vlans(vlan_ids, name)

    def _delete_vlan(self) -> None:
        vlan = self.selected_vlan()
        if vlan is None or vlan.is_default:
            return

        message = f"Delete VLAN {vlan.vlan_id}" + (f" ({vlan.name})?" if vlan.name else "?")
        informative = "This cannot be undone."
        if vlan.interfaces:
            informative = (
                f"{len(vlan.interfaces)} port(s) are assigned to this VLAN and "
                f"will stop forwarding traffic until they are reassigned.\n\n"
                + informative)

        confirm = QMessageBox(self)
        confirm.setIcon(QMessageBox.Icon.Warning)
        confirm.setWindowTitle("Delete VLAN")
        confirm.setText(message)
        confirm.setInformativeText(informative)
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes)
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if confirm.exec() == QMessageBox.StandardButton.Yes:
            self.service.delete_vlan(vlan.vlan_id)

    def _apply_assignment(self) -> None:
        ports = self.selected_ports()
        if not ports:
            return

        if self.mode_combo.currentData() == "access":
            vlan_id = self.access_vlan_combo.currentData()
            if vlan_id is None:
                return
            voice = self.voice_vlan_spin.value() or None
            self.service.assign_access_port(ports, int(vlan_id), voice)
        else:
            allowed = self.allowed_edit.text().strip()
            if allowed and allowed.lower() != "all":
                _, problem = parse_vlan.parse_vlan_range(allowed)
                if problem:
                    QMessageBox.warning(self, "Invalid VLAN List", problem)
                    return
            native = self.native_spin.value() or None
            self.service.assign_trunk_port(ports, allowed, native)

        self.status_label.setText(f"Applying changes to {len(ports)} port(s)…")

    def _show_context_menu(self, position) -> None:
        vlan = self.selected_vlan()
        if vlan is None:
            return
        menu = QMenu(self)
        menu.addAction("Create VLAN…", self._create_vlan)
        if not vlan.is_default:
            menu.addAction(f"Delete VLAN {vlan.vlan_id}…", self._delete_vlan)
        if vlan.interfaces:
            menu.addSeparator()
            menu.addAction(
                f"Select this VLAN's {len(vlan.interfaces)} port(s)",
                lambda: self.preselect_ports(vlan.interfaces))
        menu.exec(self.table.viewport().mapToGlobal(position))


class CreateVlanDialog(QDialog):
    """Collects a VLAN id (or range) and an optional name."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create VLAN")
        self.setModal(True)
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("e.g. 10  or  10,20,30-35")
        self.id_edit.textChanged.connect(self._validate)
        form.addRow("VLAN ID(s)", self.id_edit)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Optional — no spaces")
        self.name_edit.textChanged.connect(self._validate)
        form.addRow("Name", self.name_edit)

        layout.addLayout(form)

        self.hint_label = QLabel(
            "Enter a single ID, a comma-separated list, or a range. "
            "When creating several, the name becomes a prefix.")
        self.hint_label.setProperty("muted", True)
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)

        self.error_label = QLabel()
        self.error_label.setProperty("error", True)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText("Create")
        self.ok_button.setProperty("accent", True)
        self.ok_button.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._vlan_ids: list[int] = []

    def _validate(self) -> None:
        ids, problem = parse_vlan.parse_vlan_range(self.id_edit.text())

        if not problem:
            for vlan_id in ids:
                problem = parse_vlan.validate_vlan_id(vlan_id)
                if problem:
                    break
        if not problem:
            problem = parse_vlan.validate_vlan_name(self.name_edit.text())

        self._vlan_ids = ids if not problem else []
        self.error_label.setText(problem)
        self.error_label.setVisible(bool(problem))
        self.ok_button.setEnabled(bool(ids) and not problem)

        if ids and not problem and len(ids) > 1:
            self.hint_label.setText(f"Will create {len(ids)} VLANs: "
                                    + ", ".join(str(v) for v in ids[:12])
                                    + ("…" if len(ids) > 12 else ""))

    def result_values(self) -> tuple[list[int], str]:
        return self._vlan_ids, self.name_edit.text().strip()
