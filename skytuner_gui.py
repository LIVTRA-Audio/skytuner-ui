r"""Small desktop remote control for the SkyTuner vendor-HID interface.

Run from the toolkit folder:

    python skytuner_gui.py

The adjacent skytuner_hid package is included; no firmware repository is needed.
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable


from skytuner_version import VERSION

from skytuner_hid import (
    Button,
    HidTrigger,
    SkyTunerError,
)


DEFAULT_SERIAL = ""
TAP_SECONDS = 0.100
LONG_SECONDS = {
    Button.DISPLAY: 0.600,
    Button.TUNING: 0.600,
    Button.COMBO: 0.900,
}
BUTTON_KEEPALIVE_SECONDS = 0.150
BUTTON_RELEASE_RETRY_SECONDS = 0.050
BUTTON_RELEASE_ATTEMPTS = 3


def send_virtual_button(
    device: HidTrigger,
    button: Button,
    duration: float,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Send one bounded press with keepalives and a reliable release."""
    remaining = duration
    release_error: Exception | None = None

    device.set_button(button, True)
    try:
        while remaining > 0.0:
            interval = min(BUTTON_KEEPALIVE_SECONDS, remaining)
            sleep(interval)
            remaining -= interval
            if remaining > 0.0:
                device.set_button(button, True)
    finally:
        for attempt in range(BUTTON_RELEASE_ATTEMPTS):
            try:
                device.set_button(button, False)
                release_error = None
                break
            except Exception as exc:
                release_error = exc
                if attempt + 1 < BUTTON_RELEASE_ATTEMPTS:
                    sleep(BUTTON_RELEASE_RETRY_SECONDS)
        if release_error is not None:
            raise release_error


class SkyTunerRemote(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"SkyTuner UI Toolkit {VERSION}")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        self._messages: queue.Queue[tuple[str, str]] = queue.Queue()
        self._action_buttons: list[ttk.Button] = []
        self._busy = False

        self.serial = tk.StringVar(value=DEFAULT_SERIAL)
        self.status = tk.StringVar(value="Not checked")
        self.custom_ui_path = tk.StringVar()
        self.preview_scene = tk.StringVar(value="tune")

        self._build_window()
        self.after(50, self._drain_messages)
        self.after(150, self.check_device)

    def _build_window(self) -> None:
        outer = ttk.Frame(self, padding=14)
        outer.grid(sticky="nsew")

        ttk.Label(outer, text="SkyTuner UI Toolkit", font=("Segoe UI", 15, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 10)
        )

        ttk.Label(outer, text="USB ID (optional):").grid(row=1, column=0, sticky="w")
        ttk.Entry(outer, textvariable=self.serial, width=22).grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=(6, 8)
        )
        refresh = ttk.Button(outer, text="Connect / Refresh", command=self.check_device)
        refresh.grid(row=1, column=3, sticky="ew")
        self._action_buttons.append(refresh)

        ttk.Label(outer, text="Leave blank when one SkyTuner is connected.",
                  foreground="#555555").grid(row=2, column=1, columnspan=3, sticky="w")

        ttk.Label(outer, text="Device:").grid(row=3, column=0, sticky="w", pady=(8, 12))
        ttk.Label(outer, textvariable=self.status).grid(
            row=3, column=1, columnspan=3, sticky="w", pady=(8, 12)
        )

        ttk.Separator(outer).grid(
            row=4, column=0, columnspan=4, sticky="ew", pady=(0, 10)
        )

        self._add_control_row(
            outer,
            row=5,
            title="Display",
            description="TUNE: A4 down    GAME: key / pause",
            button=Button.DISPLAY,
        )
        self._add_control_row(
            outer,
            row=6,
            title="Tuning",
            description="TUNE: A4 up      GAME: scale / return",
            button=Button.TUNING,
        )
        self._add_control_row(
            outer,
            row=7,
            title="Combo",
            description="Tap: next mode / setting    Long: open / save Settings",
            button=Button.COMBO,
        )

        ttk.Label(
            outer,
            text="Combo Tap is useful while the Settings screen is open.",
            foreground="#555555",
        ).grid(row=8, column=0, columnspan=4, sticky="w", pady=(8, 10))

        custom = ttk.LabelFrame(outer, text="Custom tuner UI", padding=8)
        custom.grid(row=9, column=0, columnspan=4, sticky="ew", pady=(0, 10))
        ttk.Entry(custom, textvariable=self.custom_ui_path, width=45).grid(
            row=0, column=0, columnspan=3, sticky="ew", padx=(0, 6))
        for column, (label, command) in enumerate((
            ("Choose Script / Package", self.choose_custom_ui),
            ("Preview", self.preview_custom_ui),
            ("Upload", self.upload_custom_ui),
            ("Restore Default", self.reset_custom_ui),
        )):
            control = ttk.Button(custom, text=label, command=command)
            control.grid(row=1, column=column, padx=3, pady=(6, 0), sticky="ew")
            self._action_buttons.append(control)
        ttk.Label(custom, text="Preview screen:").grid(row=2, column=0, pady=(6, 0))
        ttk.Combobox(custom, textvariable=self.preview_scene, state="readonly",
                     values=("tune", "poly", "meter", "boot"), width=12).grid(
                         row=2, column=1, sticky="w", pady=(6, 0))

        log_bar = ttk.Frame(outer)
        log_bar.grid(row=10, column=0, columnspan=4, sticky="ew")
        ttk.Label(log_bar, text="Activity").pack(side="left")
        read_logs = ttk.Button(log_bar, text="Read Device Log", command=self.read_logs)
        read_logs.pack(side="right")
        self._action_buttons.append(read_logs)

        self.log = tk.Text(
            outer,
            width=72,
            height=10,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
        )
        self.log.grid(row=11, column=0, columnspan=4, sticky="ew", pady=(5, 0))

        for column in range(4):
            outer.columnconfigure(column, weight=1)

    def _add_control_row(
        self,
        parent: ttk.Frame,
        *,
        row: int,
        title: str,
        description: str,
        button: Button,
    ) -> None:
        ttk.Label(parent, text=title, font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, sticky="w", pady=5
        )
        ttk.Label(parent, text=description).grid(
            row=row, column=1, sticky="w", padx=(6, 10), pady=5
        )
        tap = ttk.Button(
            parent,
            text="Tap",
            command=lambda selected=button: self.send_button(selected, False),
        )
        tap.grid(row=row, column=2, sticky="ew", padx=(4, 4), pady=5)
        long_press = ttk.Button(
            parent,
            text="Long",
            command=lambda selected=button: self.send_button(selected, True),
        )
        long_press.grid(row=row, column=3, sticky="ew", padx=(4, 0), pady=5)
        self._action_buttons.extend((tap, long_press))

    @staticmethod
    def _find_device(serial_number: str | None) -> HidTrigger:
        return HidTrigger.find(serial_number=serial_number, timeout_ms=1200)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in self._action_buttons:
            button.configure(state=state)

    def _run_async(self, label: str, operation: Callable[[], str]) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self.status.set(f"{label}…")

        def worker() -> None:
            try:
                result = operation()
            except (SkyTunerError, OSError, ValueError) as exc:
                self._messages.put(("error", str(exc)))
            except Exception as exc:  # Keep unexpected HID/backend errors visible.
                self._messages.put(("error", f"{type(exc).__name__}: {exc}"))
            else:
                self._messages.put(("ok", result))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_messages(self) -> None:
        try:
            while True:
                kind, text = self._messages.get_nowait()
                if kind == "progress":
                    self.status.set(text)
                    continue
                self._set_busy(False)
                if kind == "error":
                    self.status.set("Device error")
                    self._append_log(f"ERROR: {text}")
                    messagebox.showerror("SkyTuner HID", text)
                else:
                    self.status.set(text.splitlines()[0])
                    self._append_log(text)
        except queue.Empty:
            pass
        self.after(50, self._drain_messages)

    def _append_log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{stamp}] {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def check_device(self) -> None:
        serial_number = self.serial.get().strip() or None

        def operation() -> str:
            with self._find_device(serial_number) as device:
                info = device.get_info()
            return "Connected: SkyTuner"

        self._run_async("Connecting", operation)

    def send_button(self, button: Button, long_press: bool) -> None:
        duration = LONG_SECONDS[button] if long_press else TAP_SECONDS
        action = "Long" if long_press else "Tap"
        name = button.name.title()
        serial_number = self.serial.get().strip() or None

        def operation() -> str:
            with self._find_device(serial_number) as device:
                send_virtual_button(device, button, duration)
            return f"{name} {action} sent ({duration * 1000:.0f} ms)"

        self._run_async(f"Sending {name} {action}", operation)

    def read_logs(self) -> None:
        serial_number = self.serial.get().strip() or None

        def operation() -> str:
            records: list[str] = []
            cursor = None
            with self._find_device(serial_number) as device:
                for _ in range(33):
                    chunk = device.read_log(cursor)
                    if chunk.data:
                        records.append(chunk.data)
                    cursor = chunk.next
                    if cursor == chunk.tail:
                        break
            if not records:
                return "Device log is empty"
            return "Device log:\n  " + "\n  ".join(records)

        self._run_async("Reading log", operation)

    def choose_custom_ui(self) -> None:
        selected = filedialog.askopenfilename(
            title="Choose a custom UI script or compiled package",
            filetypes=(("SkyTuner UI", "*.py *.pdui"), ("All files", "*.*")),
            initialdir=str(Path(__file__).resolve().parent / "examples"),
        )
        if selected:
            self.custom_ui_path.set(selected)

    def preview_custom_ui(self) -> None:
        try:
            from PIL import ImageTk
            from skytuner_ui import load_package, render_preview
            data = load_package(self.custom_ui_path.get())
            scene = self.preview_scene.get()
            preview = render_preview(data, scene=scene).resize((256, 320))
            window = tk.Toplevel(self)
            window.title(f"SkyTuner {scene} preview — E, +12 cents")
            photo = ImageTk.PhotoImage(preview)
            label = ttk.Label(window, image=photo)
            label.image = photo
            label.pack(padx=12, pady=12)
            ttk.Label(window, text="Header/footer approximate; center uses compiled assets.").pack(padx=12, pady=(0, 12))
        except (ImportError, OSError, ValueError) as exc:
            messagebox.showerror("Custom UI preview", str(exc))

    def upload_custom_ui(self) -> None:
        serial = self.serial.get().strip()
        path = self.custom_ui_path.get()
        if not serial:
            messagebox.showerror("Custom UI", "Enter the exact HID serial before uploading.")
            return

        def operation() -> str:
            from skytuner_ui import load_package, Uploader
            data = load_package(path)
            def progress(status):
                percent = status.received * 100 // max(status.expected, 1)
                self._messages.put(("progress", f"Custom UI upload: {percent}%"))
            with self._find_device(serial) as device:
                status = Uploader(device, progress=progress).upload(data)
            return f"Custom UI installed: generation {status.generation}, {status.expected} bytes"
        self._run_async("Uploading custom UI", operation)

    def reset_custom_ui(self) -> None:
        serial = self.serial.get().strip()
        if not serial:
            messagebox.showerror("Custom UI", "Enter the exact HID serial before restoring defaults.")
            return

        def operation() -> str:
            from skytuner_ui import Uploader
            with self._find_device(serial) as device:
                status = Uploader(device).reset()
            return f"Built-in UI restored: generation {status.generation}"
        self._run_async("Restoring built-in UI", operation)


def main() -> int:
    try:
        app = SkyTunerRemote()
    except tk.TclError as exc:
        print(f"Unable to start Tkinter GUI: {exc}", file=sys.stderr)
        return 2
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
