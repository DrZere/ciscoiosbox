"""Screenshot capture for the demo, used by ``python demo.py --screenshots``.

Renders each tab offscreen once its data has loaded, so the images show a
populated interface rather than empty tables.
"""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer

OUTPUT_DIR = Path("demo_screenshots")


def pump(seconds: float) -> None:
    """Run the event loop for a while so pending work completes and repaints."""
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec()


def wait_for(condition, timeout: float = 12.0) -> bool:
    loop = QEventLoop()
    deadline = time.monotonic() + timeout

    def check():
        if condition() or time.monotonic() > deadline:
            loop.quit()

    timer = QTimer()
    timer.timeout.connect(check)
    timer.start(20)
    loop.exec()
    timer.stop()
    return condition()


def grab(widget, name: str) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"{name}.png"
    widget.grab().save(str(path))
    size = widget.size()
    print(f"  wrote {path}  ({size.width()}x{size.height()})")
    return path


def capture_all(app, window) -> int:
    """Drive the app through each view, capturing a screenshot of each."""
    print("Capturing screenshots to ./demo_screenshots/ …")

    pump(0.4)
    grab(window, "01-session-manager")

    profile = window.store.profiles[0]
    window.connect_to(profile)
    tab = window.current_tab()

    if not wait_for(lambda: tab.is_connected):
        print("  ERROR: the simulated device never connected")
        return 1

    # Terminal: run a couple of commands so it is not just a bare prompt.
    tab.controller.send_keys("show ip interface brief\r")
    pump(1.0)
    tab.controller.send_keys("show vlan brief\r")
    pump(1.2)
    tab.terminal._flush()
    pump(0.3)
    grab(window, "02-terminal")

    # Interfaces
    if not wait_for(lambda: tab.interfaces_view.model.rowCount() > 0):
        print("  ERROR: the interface grid never populated")
        return 1
    tab.tabs.setCurrentWidget(tab.interfaces_view)
    pump(0.5)
    # Select a few rows so the action buttons are shown enabled.
    table = tab.interfaces_view.table
    table.selectRow(0)
    table.selectionModel().select(
        table.model().index(1, 0),
        table.selectionModel().SelectionFlag.Select
        | table.selectionModel().SelectionFlag.Rows)
    pump(0.4)
    grab(window, "03-interfaces")

    # VLANs
    wait_for(lambda: tab.vlans_view.model.rowCount() > 0)
    tab.tabs.setCurrentWidget(tab.vlans_view)
    tab.vlans_view.table.selectRow(1)
    tab.vlans_view.preselect_ports([
        "GigabitEthernet1/0/4", "GigabitEthernet1/0/5"])
    pump(0.5)
    grab(window, "04-vlans")

    # System
    tab.tabs.setCurrentWidget(tab.system_view)
    tab.system_service.load_management_config()
    tab.system_service.load_running_config()
    wait_for(lambda: bool(tab.system_view.config_view.toPlainText()))
    pump(0.5)
    grab(window, "05-system")

    # Monitoring — let the graphs accumulate real samples.
    tab.tabs.setCurrentWidget(tab.monitor_view)
    tab.monitor_view.select_interface("GigabitEthernet1/0/1")
    tab.monitor_view.interval_spin.setValue(1.0)
    if not tab.monitor_view.start_button.isChecked():
        tab.monitor_view.start_button.setChecked(True)
    print("  collecting monitoring samples…")
    wait_for(lambda: tab.monitor_view.cpu_graph.sample_count >= 12
             and tab.monitor_view.traffic_graph.sample_count >= 8, timeout=25.0)
    pump(0.6)
    grab(window, "06-monitoring")
    tab.monitor_view.stop()

    # Session editor dialog.
    from ciscoiosbox.ui.session_dialog import SessionDialog

    dialog = SessionDialog(window.store.profiles[0], window)
    dialog.resize(600, 520)
    dialog.show()
    pump(0.5)
    grab(dialog, "07-session-editor")
    dialog.tabs.setCurrentIndex(2)          # SNMP tab
    pump(0.4)
    grab(dialog, "08-session-editor-snmp")
    dialog.close()

    # Shut the worker thread down before the interpreter exits, or Qt warns
    # about a QThread destroyed while still running. close_tab() returns
    # immediately (teardown happens in the background), so wait for it.
    tab.close_tab()
    wait_for(lambda: not tab.controller.thread_running, timeout=6.0)
    pump(0.3)

    print(f"\nDone — {len(list(OUTPUT_DIR.glob('*.png')))} screenshots in "
          f"{OUTPUT_DIR.resolve()}")
    return 0
