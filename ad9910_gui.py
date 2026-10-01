"""Mixed-clock GUI. Each enabled DDS chooses INTERNAL or EXTERNAL at initialization."""

from __future__ import annotations

import queue
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# Support launching either GUI directly from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from frequency_corr import corrected_frequency

try:
    import serial
    from serial.tools import list_ports
except ImportError as exc:  # pragma: no cover - gives a useful launch error
    raise SystemExit("pyserial is required: python -m pip install pyserial") from exc


BAUD_RATE = 115200
CHANNEL_COUNT = 20
MAX_FREQUENCY = 499_999_999
INITIAL_FREQUENCY = 100_000_000
PROGRAMMING_PORT_FQBN = "arduino:sam:arduino_due_x_dbg"


def frequency_in_hz(text: str, unit: str) -> int:
    """Inputs are whole Hz or whole MHz; the serial protocol always uses Hz."""
    value = int(text)
    if unit not in ("Hz", "MHz"):
        raise ValueError("Unknown frequency unit")
    hz = value * (1_000_000 if unit == "MHz" else 1)
    if not 0 <= hz <= MAX_FREQUENCY:
        raise ValueError("Frequency must be 0..499999999 Hz (0..499 whole MHz)")
    return hz


def convert_frequency(text: str, old_unit: str, new_unit: str) -> str:
    hz = frequency_in_hz(text, old_unit)
    # Integer arithmetic implements half-up rounding, not Python's ties-to-even.
    value = str((hz + 500_000) // 1_000_000) if new_unit == "MHz" else str(hz)
    frequency_in_hz(value, new_unit)  # Reject a rounded 500 MHz, above DDS limit.
    return value


@dataclass
class ChannelWidgets:
    frequency: tk.StringVar
    amplitude: tk.StringVar
    phase: tk.StringVar
    shutter: tk.BooleanVar
    widgets: list[tk.Widget]
    frequency_unit: tk.StringVar
    frequency_spin: ttk.Spinbox
    previous_unit: str = "Hz"


class AD9910App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("AD9910 Mixed-Clock Controller")
        self.root.minsize(900, 650)

        self.serial_port: serial.Serial | None = None
        self.serial_lock = threading.Lock()
        self.reader_stop = threading.Event()
        self.reader_thread: threading.Thread | None = None
        self.rx_queue: queue.Queue[str] = queue.Queue()
        self.ready_seen = False
        self.configured_count = 0
        self.config_sent = False
        self.pending_initialization = False
        self.connect_attempts = 0
        self.initializer: tk.Toplevel | None = None

        # First-launch defaults; a saved configuration overrides these below.
        self.cli_var = tk.StringVar(value="")
        self.ino_var = tk.StringVar(value="")
        self.init_status_var = tk.StringVar(value="Select the firmware, port, and DDS count.")

        self.port_var = tk.StringVar()
        self.count_var = tk.IntVar(value=20)
        self.clock_vars = [tk.StringVar(value="INTERNAL") for _ in range(CHANNEL_COUNT)]
        self.requested_map = ""
        self.active_map = ""
        self.auto_apply_var = tk.BooleanVar(value=False)
        self.connection_var = tk.StringVar(value="Disconnected")
        self.channel_rows: list[ChannelWidgets] = []

        self.settings_path = Path(__file__).resolve().parent / "initialization_config.json"
        self.pending_settings = None
        self._build_ui()
        self._load_settings()
        self.refresh_ports()
        self._set_controls_enabled(False)
        self.root.after(50, self._drain_rx_queue)
        self.root.after(0, self.show_initialization_dialog)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill=tk.X)

        ttk.Label(top, text="Programming Port:").grid(row=0, column=0, sticky="w")
        self.port_combo = ttk.Combobox(top, textvariable=self.port_var, width=30)
        self.port_combo.grid(row=0, column=1, padx=5, sticky="ew")
        ttk.Button(top, text="Refresh", command=self.refresh_ports).grid(row=0, column=2, padx=3)
        self.connect_button = ttk.Button(top, text="Connect", command=self.toggle_connection)
        self.connect_button.grid(row=0, column=3, padx=3)
        ttk.Label(top, textvariable=self.connection_var).grid(row=0, column=4, padx=10)

        ttk.Label(top, text="Enabled DDS:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.count_spin = ttk.Spinbox(
            top, from_=1, to=20, textvariable=self.count_var, width=6
        )
        self.count_spin.grid(row=1, column=1, sticky="w", padx=5, pady=(8, 0))
        self.configure_button = ttk.Button(top, text="Configure", command=self.configure_channels)
        self.configure_button.grid(row=1, column=2, padx=3, pady=(8, 0))
        self.auto_check = ttk.Checkbutton(top, text="Auto apply", variable=self.auto_apply_var)
        self.auto_check.grid(row=1, column=3, padx=3, pady=(8, 0))
        top.columnconfigure(1, weight=1)

        toolbar = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        toolbar.pack(fill=tk.X)
        self.stage_all_button = ttk.Button(toolbar, text="Set all", command=self.stage_all)
        self.stage_all_button.pack(side=tk.LEFT, padx=(0, 5))
        self.apply_button = ttk.Button(toolbar, text="APPLY ALL", command=self.apply_all)
        self.apply_button.pack(side=tk.LEFT, padx=5)
        self.status_button = ttk.Button(toolbar, text="Read status", command=lambda: self.send("STATUS"))
        self.status_button.pack(side=tk.LEFT, padx=5)
        self.reset_button = ttk.Button(
            toolbar, text="Reset to defaults", command=self.reset_firmware
        )
        self.reset_button.pack(side=tk.LEFT, padx=5)
        self.close_all_button = ttk.Button(
            toolbar, text="Close all Internal Shutters", command=self.close_all_shutters
        )
        self.close_all_button.pack(side=tk.LEFT, padx=5)
        ttk.Button(toolbar, text="Initialization...", command=self.show_initialization_dialog).pack(
            side=tk.RIGHT, padx=5
        )

        table_container = ttk.Frame(self.root, padding=(10, 0))
        table_container.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(table_container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(table_container, orient=tk.VERTICAL, command=canvas.yview)
        self.table = ttk.Frame(canvas)
        self.table.bind(
            "<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas_window = canvas.create_window((0, 0), window=self.table, anchor="nw")
        canvas.bind(
            "<Configure>", lambda event: canvas.itemconfigure(canvas_window, width=event.width)
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        headings = (
            "DDS", "Frequency", "Amplitude", "Phase (deg)",
            "Internal Shutter", "Set"
        )
        for column, heading in enumerate(headings):
            ttk.Label(self.table, text=heading, anchor="center").grid(
                row=0, column=column, padx=4, pady=4, sticky="ew"
            )
        for column in (1, 2, 3):
            self.table.columnconfigure(column, weight=1)

        for index in range(CHANNEL_COUNT):
            self._create_channel_row(index)

        log_frame = ttk.LabelFrame(self.root, text="Serial log", padding=5)
        log_frame.pack(fill=tk.BOTH, padx=10, pady=10)
        self.log = tk.Text(log_frame, height=9, state=tk.DISABLED, wrap=tk.NONE)
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _create_channel_row(self, index: int) -> None:
        row = index + 1
        frequency = tk.StringVar(value=str(INITIAL_FREQUENCY))
        amplitude = tk.StringVar(value="0")
        phase = tk.StringVar(value="0")
        shutter = tk.BooleanVar(value=False)
        widgets: list[tk.Widget] = []

        label = ttk.Label(self.table, text=str(row), anchor="center")
        label.grid(row=row, column=0, padx=4, pady=2, sticky="ew")
        widgets.append(label)
        frequency_frame = ttk.Frame(self.table)
        frequency_frame.grid(row=row, column=1, padx=4, pady=2, sticky="ew")
        frequency_frame.columnconfigure(0, weight=1)
        # Native spinbox arrows repeat while held; values do not wrap at limits.
        frequency_entry = ttk.Spinbox(
            frequency_frame, textvariable=frequency, justify=tk.RIGHT,
            from_=0, to=MAX_FREQUENCY, increment=1, format="%.0f",
            command=lambda ch=index: self._arrow_changed(ch),
        )
        frequency_entry.grid(row=0, column=0, sticky="ew")
        frequency_entry.bind("<Return>", lambda _event, ch=index: self.stage_channel(ch))
        unit = tk.StringVar(value="Hz")
        unit_selector = ttk.Combobox(
            frequency_frame, textvariable=unit, values=("Hz", "MHz"),
            state="readonly", width=5,
        )
        unit_selector.grid(row=0, column=1, padx=(4, 0))
        unit_selector.bind("<<ComboboxSelected>>", lambda _event, ch=index: self.change_frequency_unit(ch))
        widgets.extend((frequency_entry, unit_selector))
        for column, variable in ((2, amplitude), (3, phase)):
            entry = ttk.Spinbox(
                self.table, textvariable=variable, justify=tk.RIGHT,
                from_=0, to=1 if column == 2 else 359.999999,
                increment=0.01 if column == 2 else 1, format="%.6f",
                command=lambda ch=index: self._arrow_changed(ch),
            )
            entry.grid(row=row, column=column, padx=4, pady=2, sticky="ew")
            entry.bind("<Return>", lambda _event, ch=index: self.stage_channel(ch))
            widgets.append(entry)
        shutter_button = ttk.Checkbutton(
            self.table,
            text="Open",
            variable=shutter,
            command=lambda ch=index: self.set_shutter(ch),
        )
        shutter_button.grid(row=row, column=4, padx=4, pady=2)
        widgets.append(shutter_button)
        set_button = ttk.Button(
            self.table, text="Set", command=lambda ch=index: self.stage_channel(ch)
        )
        set_button.grid(row=row, column=5, padx=4, pady=2)
        widgets.append(set_button)
        self.channel_rows.append(
            ChannelWidgets(frequency, amplitude, phase, shutter, widgets, unit, frequency_entry)
        )

    def _arrow_changed(self, index: int) -> None:
        """Each arrow/repeat updates hardware only when Auto apply is checked."""
        if self.auto_apply_var.get():
            self.stage_channel(index)

    def change_frequency_unit(self, index: int) -> None:
        """Convert the editable value only; Set/APPLY controls hardware updates."""
        row = self.channel_rows[index]
        selected = row.frequency_unit.get()
        try:
            value = convert_frequency(row.frequency.get(), row.previous_unit, selected)
        except ValueError as exc:
            row.frequency_unit.set(row.previous_unit)
            messagebox.showerror("Frequency conversion", str(exc))
            return
        row.frequency.set(value)
        row.previous_unit = selected
        row.frequency_spin.configure(
            to=MAX_FREQUENCY // 1_000_000 if selected == "MHz" else MAX_FREQUENCY
        )


    def _settings_snapshot(self) -> dict:
        """Capture initialization inputs before asynchronous upload/configuration."""
        return {
            "version": 1,
            "cli_path": self.cli_var.get(),
            "firmware_path": self.ino_var.get(),
            "port": self.port_var.get(),
            "dds_count": int(self.count_var.get()),
            "clock_modes": [v.get() for v in self.clock_vars],
        }

    def _load_settings(self) -> None:
        """Invalid or missing saved settings leave the built-in defaults intact."""
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("Unsupported settings format")
            for key, variable in (("cli_path", self.cli_var), ("firmware_path", self.ino_var),
                                  ("port", self.port_var)):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    variable.set(value)
            count = data.get("dds_count")
            if type(count) is int and 1 <= count <= CHANNEL_COUNT:
                self.count_var.set(count)
            modes = data.get("clock_modes", [])
            if isinstance(modes, list):
                for variable, mode in zip(self.clock_vars, modes):
                    if mode in ("INTERNAL", "EXTERNAL"):
                        variable.set(mode)
            # Units and Auto apply are session-only: always start in Hz / off.
            # Ignore these fields if present in a config saved by an older GUI.
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            self._append_log(f"# Could not load initialization settings: {exc}")

    def _save_settings(self) -> None:
        """Only a successful CONFIG acknowledgement commits the last attempt."""
        if self.pending_settings is None:
            return
        try:
            # Replace atomically so interrupted writes cannot corrupt the previous config.
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.settings_path.parent,
                prefix=".initialization-", suffix=".tmp", delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(self.pending_settings, handle, indent=2)
                handle.write("\n")
            temp_path.replace(self.settings_path)
            self._append_log(f"# Saved settings: {self.settings_path}")
        except OSError as exc:
            self._append_log(f"# Settings could not be saved: {exc}")
        finally:
            self.pending_settings = None

    def refresh_ports(self) -> None:
        ports = [port.device for port in list_ports.comports()]
        current = self.port_var.get()
        self.port_combo["values"] = ports
        if current:
            self.port_var.set(current)
        elif ports:
            self.port_var.set(ports[0])
        else:
            self.port_var.set("")
        if hasattr(self, "init_port_combo") and self.init_port_combo.winfo_exists():
            self.init_port_combo["values"] = ports

    def show_initialization_dialog(self) -> None:
        if self.initializer and self.initializer.winfo_exists():
            self.initializer.lift()
            self.initializer.focus_force()
            return

        dialog = tk.Toplevel(self.root)
        self.initializer = dialog
        dialog.title("Initialize AD9910 system")
        dialog.resizable(True, False)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.protocol("WM_DELETE_WINDOW", self._close_initializer)

        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Arduino CLI:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.cli_var, width=65).grid(
            row=0, column=1, sticky="ew", padx=6, pady=4
        )
        ttk.Button(frame, text="Browse...", command=self._browse_cli).grid(row=0, column=2, pady=4)

        ttk.Label(frame, text="Firmware (.ino):").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frame, textvariable=self.ino_var).grid(
            row=1, column=1, sticky="ew", padx=6, pady=4
        )
        ttk.Button(frame, text="Browse...", command=self._browse_ino).grid(row=1, column=2, pady=4)

        ttk.Label(frame, text="Programming Port:").grid(row=2, column=0, sticky="w", pady=4)
        self.init_port_combo = ttk.Combobox(frame, textvariable=self.port_var, width=25)
        self.init_port_combo.grid(row=2, column=1, sticky="w", padx=6, pady=4)
        ttk.Button(frame, text="Refresh", command=self.refresh_ports).grid(row=2, column=2, pady=4)

        ttk.Label(frame, text="Enabled DDS:").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Spinbox(frame, from_=1, to=20, textvariable=self.count_var, width=8).grid(
            row=3, column=1, sticky="w", padx=6, pady=4
        )

        # Two columns keep the full 20-channel clock map visible.
        clocks = ttk.LabelFrame(frame, text="Per-DDS clock (only DDS 1..N are used)", padding=6)
        clocks.grid(row=4, column=0, columnspan=3, sticky="ew", pady=6)
        clock_buttons = ttk.Frame(clocks)
        clock_buttons.grid(row=10, column=0, columnspan=4, sticky="w", padx=8, pady=(8, 0))
        ttk.Button(
            clock_buttons, text="All INTERNAL", command=lambda: self._set_all_clocks("INTERNAL")
        ).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(
            clock_buttons, text="All EXTERNAL", command=lambda: self._set_all_clocks("EXTERNAL")
        ).pack(side=tk.LEFT)
        for index, variable in enumerate(self.clock_vars):
            column = (index // 10) * 2
            ttk.Label(clocks, text=f"DDS {index + 1}").grid(row=index % 10, column=column, padx=8)
            ttk.Combobox(
                clocks, textvariable=variable, values=("INTERNAL", "EXTERNAL"),
                state="readonly", width=16,
            ).grid(row=index % 10, column=column + 1, padx=8, pady=1)

        ttk.Label(
            frame,
            text=(
                "Target: Arduino Due (Programming Port)\n"
                "INTERNAL: 40 MHz reference + PLL x25.\n"
                "EXTERNAL: direct 1 GHz reference; PLL disabled.\n"
                "Initialization: F=100 MHz, A=0, P=0; enabled Internal Shutters open."
            ),
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 4))
        ttk.Label(frame, textvariable=self.init_status_var, foreground="#205080").grid(
            row=6, column=0, columnspan=3, sticky="w", pady=4
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self._close_initializer).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        self.initialize_button = ttk.Button(
            buttons, text="Compile, upload & initialize", command=self.start_initialization
        )
        self.initialize_button.pack(side=tk.RIGHT)
        self.refresh_ports()

    def _set_all_clocks(self, mode: str) -> None:
        """Change all selections; hardware is configured only on submission."""
        for variable in self.clock_vars:
            variable.set(mode)

    def _close_initializer(self) -> None:
        if self.pending_initialization:
            messagebox.showinfo("Initialization", "Wait for initialization to finish.")
            return
        if self.initializer and self.initializer.winfo_exists():
            self.initializer.grab_release()
            self.initializer.destroy()
        self.initializer = None

    def _browse_cli(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.initializer,
            title="Select arduino-cli executable",
            filetypes=(("Executable", "*.exe"), ("All files", "*.*")),
        )
        if path:
            self.cli_var.set(path)

    def _browse_ino(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.initializer,
            title="Select Arduino firmware",
            filetypes=(("Arduino sketch", "*.ino"), ("All files", "*.*")),
        )
        if path:
            self.ino_var.set(path)

    def start_initialization(self) -> None:
        cli_text = self.cli_var.get().strip()
        cli_path = shutil.which(cli_text) or cli_text
        ino_path = Path(self.ino_var.get().strip()).expanduser()
        port = self.port_var.get().strip()
        try:
            count = int(self.count_var.get())
        except (ValueError, tk.TclError):
            count = 0

        if not cli_text or (not shutil.which(cli_text) and not Path(cli_text).is_file()):
            messagebox.showerror("Arduino CLI", "Select a valid arduino-cli executable.")
            return
        if not ino_path.is_file() or ino_path.suffix.lower() != ".ino":
            messagebox.showerror("Firmware", "Select a valid .ino file.")
            return
        if ino_path.parent.name != ino_path.stem:
            messagebox.showerror(
                "Firmware",
                "Arduino requires the .ino filename to match its containing folder name.",
            )
            return
        if not port:
            messagebox.showerror("Programming Port", "Select the Arduino Due Programming Port.")
            return
        if not 1 <= count <= CHANNEL_COUNT:
            messagebox.showerror("DDS count", "Enabled DDS count must be 1..20.")
            return
        if any(v.get() not in ("INTERNAL", "EXTERNAL") for v in self.clock_vars[:count]):
            messagebox.showerror("Clock mode", "Select INTERNAL or EXTERNAL clock mode.")
            return

        if self.serial_port and self.serial_port.is_open:
            self.disconnect()
        self.requested_map = self._clock_map(count)
        self.pending_settings = self._settings_snapshot()
        self.pending_initialization = True
        self.config_sent = False
        self.init_status_var.set("Compiling firmware...")
        self.initialize_button.configure(state=tk.DISABLED)
        worker = threading.Thread(
            target=self._compile_and_upload,
            args=(str(cli_path), ino_path, port, count),
            daemon=True,
        )
        worker.start()

    def _run_cli_command(self, command: list[str]) -> None:
        self.rx_queue.put("# CLI " + " ".join(command))
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert process.stdout is not None
        for line in process.stdout:
            self.rx_queue.put("# CLI " + line.rstrip())
        return_code = process.wait()
        if return_code != 0:
            raise RuntimeError(f"arduino-cli exited with status {return_code}")

    def _compile_and_upload(self, cli: str, ino_path: Path, port: str, count: int) -> None:
        sketch_dir = str(ino_path.parent)
        try:
            with tempfile.TemporaryDirectory(prefix="ad9910_build_") as build_dir:
                self._run_cli_command(
                    [
                        cli, "compile", "--fqbn", PROGRAMMING_PORT_FQBN,
                        "--output-dir", build_dir, sketch_dir,
                    ]
                )
                self.root.after(0, lambda: self.init_status_var.set("Uploading firmware..."))
                self._run_cli_command(
                    [
                        cli, "upload", "--port", port, "--fqbn",
                        PROGRAMMING_PORT_FQBN, "--input-dir", build_dir, sketch_dir,
                    ]
                )
        except (OSError, RuntimeError) as exc:
            self.root.after(0, lambda error=str(exc): self._initialization_failed(error))
            return
        self.root.after(0, lambda: self._after_upload(port, count))

    def _initialization_failed(self, error: str) -> None:
        self.pending_initialization = False
        self.init_status_var.set("Initialization failed; see the serial/CLI log.")
        if self.initializer and self.initializer.winfo_exists():
            self.initialize_button.configure(state=tk.NORMAL)
        messagebox.showerror(
            "Compile/upload failed",
            error + "\n\nConfirm Arduino CLI and the arduino:sam core are installed.",
        )

    def _after_upload(self, port: str, count: int) -> None:
        self.port_var.set(port)
        self.count_var.set(count)
        self.init_status_var.set("Upload complete; waiting for firmware READY...")
        # Allow Windows time to re-enumerate the Programming Port after reset.
        self.connect_attempts = 0
        self.root.after(1000, self.connect)

    def toggle_connection(self) -> None:
        if self.serial_port and self.serial_port.is_open:
            self.disconnect()
        else:
            self.connect()

    def connect(self) -> None:
        port = self.port_var.get().strip()
        if not port:
            messagebox.showerror("Connection", "Select the Arduino Due Programming Port.")
            return
        try:
            connection = serial.Serial(port, BAUD_RATE, timeout=0.2, write_timeout=1)
        except serial.SerialException as exc:
            if self.pending_initialization:
                self.connect_attempts += 1
                if self.connect_attempts < 6:
                    self.init_status_var.set(
                        f"Waiting for Programming Port ({self.connect_attempts}/5)..."
                    )
                    self.root.after(1000, self.connect)
                    return
                self._initialization_failed(str(exc))
                return
            messagebox.showerror("Connection failed", str(exc))
            return
        self.serial_port = connection
        self.connect_attempts = 0
        self.ready_seen = False
        self.configured_count = 0
        self.config_sent = False
        self.reader_stop.clear()
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()
        self.connection_var.set(f"Connected: {port}; waiting for READY")
        self.connect_button.configure(text="Disconnect")
        self._set_controls_enabled(False)
        self._append_log(f"# Connected to {port} at {BAUD_RATE}")
        # Programming Port normally resets the Due. HELLO is a fallback if READY
        # was emitted before the host opened the port.
        self.root.after(1800, self._request_handshake)

    def _request_handshake(self) -> None:
        if self.serial_port and self.serial_port.is_open and not self.ready_seen:
            self.send("HELLO")

    def disconnect(self) -> None:
        # Best effort safety action before closing the transport.
        if self.serial_port and self.serial_port.is_open:
            for number in range(1, self.configured_count + 1):
                try:
                    self._write_line(f"SHUTTER {number} 0", log=False)
                except serial.SerialException:
                    break
        self.reader_stop.set()
        connection = self.serial_port
        self.serial_port = None
        if connection:
            try:
                connection.close()
            except serial.SerialException:
                pass
        self.configured_count = 0
        self.ready_seen = False
        self.connection_var.set("Disconnected")
        self.connect_button.configure(text="Connect")
        self._set_controls_enabled(False)
        self._append_log("# Disconnected")

    def _reader_loop(self) -> None:
        connection = self.serial_port
        if connection is None:
            return
        try:
            while not self.reader_stop.is_set() and connection.is_open:
                raw = connection.readline()
                if raw:
                    self.rx_queue.put(raw.decode("utf-8", errors="replace").strip())
        except serial.SerialException as exc:
            self.rx_queue.put(f"# SERIAL_ERROR {exc}")

    def _drain_rx_queue(self) -> None:
        try:
            while True:
                line = self.rx_queue.get_nowait()
                self._handle_received_line(line)
        except queue.Empty:
            pass
        self.root.after(50, self._drain_rx_queue)

    def _handle_received_line(self, line: str) -> None:
        if not line:
            return
        self._append_log(f"< {line}")
        if line.startswith("# SERIAL_ERROR"):
            was_initializing = self.pending_initialization
            self.disconnect()
            if was_initializing:
                self._initialization_failed(line)
            else:
                messagebox.showerror("Serial connection", line)
            return
        if line.startswith("READY") or line.startswith("OK HELLO"):
            # Reject the older global-clock firmware before configuring hardware.
            if "AD9910_DUE_MIXED" not in line or "PROTOCOL=3" not in line:
                if self.pending_initialization:
                    self._initialization_failed("Select the mixed-clock PROTOCOL=3 firmware.")
                return
            if not self.ready_seen:
                self.ready_seen = True
                self.connection_var.set("Connected; configure DDS count")
                self.configure_button.configure(state=tk.NORMAL)
                self.count_spin.configure(state=tk.NORMAL)
                self.send("HELLO") if line.startswith("READY") else None
            if self.pending_initialization and not self.config_sent:
                self.init_status_var.set("Firmware ready; configuring DDS channels...")
                self.config_sent = self.send(
                    f"CONFIG {len(self.requested_map)} {self.requested_map}"
                )
                self.root.after(5000, self._config_timeout)
        elif line.startswith("OK CONFIG"):
            fields = dict(token.split("=", 1) for token in line.split() if "=" in token)
            if fields.get("CLOCKS") != self.requested_map:
                self.connection_var.set("Clock confirmation mismatch; configuration rejected")
                if self.pending_initialization:
                    self._initialization_failed("Firmware did not confirm the requested clock map.")
                return
            self.active_map = fields["CLOCKS"]
            self.configured_count = int(fields["ACTIVE"])
            self._save_settings()
            self._reset_gui_values()
            self._set_controls_enabled(True)
            self.connection_var.set(
                f"Ready: DDS 1-{self.configured_count}; clocks={self.active_map}"
            )
            if self.pending_initialization:
                self.pending_initialization = False
                self.config_sent = False
                self.init_status_var.set("Initialization complete.")
                self._close_initializer()
        elif line.startswith("OK RESET"):
            self._reset_gui_values()
        elif line.startswith("ERR"):
            self.connection_var.set(line)
            if self.pending_initialization:
                self.pending_initialization = False
                self.config_sent = False
                self.init_status_var.set("Firmware configuration failed; see log.")
                if self.initializer and self.initializer.winfo_exists():
                    self.initialize_button.configure(state=tk.NORMAL)

    def _write_line(self, command: str, *, log: bool = True) -> None:
        connection = self.serial_port
        if connection is None or not connection.is_open:
            raise serial.SerialException("Serial port is not connected")
        with self.serial_lock:
            connection.write((command + "\n").encode("ascii"))
            connection.flush()
        if log:
            self._append_log(f"> {command}")

    def send(self, command: str) -> bool:
        try:
            self._write_line(command)
            return True
        except serial.SerialException as exc:
            messagebox.showerror("Serial write failed", str(exc))
            return False

    def _config_timeout(self) -> None:
        if self.pending_initialization and self.config_sent:
            self._initialization_failed("No CONFIG acknowledgement within 5 seconds. See serial log.")

    def _clock_map(self, count: int) -> str:
        """Encode user-visible choices into one atomic CONFIG command."""
        return "".join("I" if v.get() == "INTERNAL" else "E" for v in self.clock_vars[:count])

    def configure_channels(self) -> None:
        try:
            count = int(self.count_var.get())
        except (ValueError, tk.TclError):
            messagebox.showerror("Configuration", "Enabled DDS count must be 1..20.")
            return
        if not 1 <= count <= CHANNEL_COUNT:
            messagebox.showerror("Configuration", "Enabled DDS count must be 1..20.")
            return
        # CONFIG closes all shutters, resets every DDS, initializes the selected
        # prefix to F=100 MHz/A=0/P=0, then opens their Internal Shutters.
        self._set_controls_enabled(False)
        self.requested_map = self._clock_map(count)
        self.pending_settings = self._settings_snapshot()
        self.send(f"CONFIG {count} {self.requested_map}")

    def _validated_values(self, index: int) -> tuple[int, float, float] | None:
        row = self.channel_rows[index]
        try:
            frequency = frequency_in_hz(row.frequency.get(), row.frequency_unit.get())
            amplitude = float(row.amplitude.get())
            phase = float(row.phase.get())
        except ValueError:
            messagebox.showerror("Invalid value", f"DDS {index + 1}: enter numeric values.")
            return None
        try:
            frequency = corrected_frequency(frequency, index + 1, MAX_FREQUENCY)
        except (ValueError, OSError, UnicodeError) as exc:
            messagebox.showerror("Frequency correction failed", f"DDS {index + 1}: {exc}")
            return None
        if not 0.0 <= amplitude <= 1.0:
            messagebox.showerror("Invalid amplitude", f"DDS {index + 1}: use 0..1.")
            return None
        if not 0.0 <= phase < 360.0:
            messagebox.showerror("Invalid phase", f"DDS {index + 1}: use 0..359.999999 degrees.")
            return None
        return frequency, amplitude, phase

    def stage_channel(self, index: int, *, apply_if_enabled: bool = True) -> bool:
        if index >= self.configured_count:
            return False
        values = self._validated_values(index)
        if values is None:
            return False
        frequency, amplitude, phase = values
        number = index + 1
        commands = (
            f"F {number} {frequency}",
            f"A {number} {amplitude:.9g}",
            f"P {number} {phase:.9g}",
        )
        if not all(self.send(command) for command in commands):
            return False
        if apply_if_enabled and self.auto_apply_var.get():
            return self.send("APPLY ALL")
        return True

    def stage_all(self, *, apply_if_enabled: bool = True) -> bool:
        for index in range(self.configured_count):
            if not self.stage_channel(index, apply_if_enabled=False):
                return False
        if apply_if_enabled and self.auto_apply_var.get():
            return self.send("APPLY ALL")
        return True

    def apply_all(self) -> None:
        # Synchronize every GUI field into the DDS buffers, then issue exactly
        # one common IO_UPDATE pulse.
        if self.stage_all(apply_if_enabled=False):
            self.send("APPLY ALL")

    def set_shutter(self, index: int) -> None:
        if index >= self.configured_count:
            self.channel_rows[index].shutter.set(False)
            return
        value = 1 if self.channel_rows[index].shutter.get() else 0
        if not self.send(f"SHUTTER {index + 1} {value}"):
            self.channel_rows[index].shutter.set(not bool(value))

    def close_all_shutters(self) -> None:
        for index in range(self.configured_count):
            self.channel_rows[index].shutter.set(False)
            if not self.send(f"SHUTTER {index + 1} 0"):
                break

    def reset_firmware(self) -> None:
        self.send("RESET")

    def _reset_gui_values(self) -> None:
        for index, row in enumerate(self.channel_rows):
            row.frequency.set(str(INITIAL_FREQUENCY // 1_000_000)
                              if row.frequency_unit.get() == "MHz" else str(INITIAL_FREQUENCY))
            row.amplitude.set("0")
            row.phase.set("0")
            row.shutter.set(index < self.configured_count)

    def _set_controls_enabled(self, configured: bool) -> None:
        connected = bool(self.serial_port and self.serial_port.is_open)
        self.configure_button.configure(state=tk.NORMAL if connected and self.ready_seen else tk.DISABLED)
        self.count_spin.configure(state=tk.NORMAL if connected and self.ready_seen else tk.DISABLED)
        common_state = tk.NORMAL if connected and configured else tk.DISABLED
        for button in (
            self.stage_all_button,
            self.apply_button,
            self.status_button,
            self.reset_button,
            self.close_all_button,
            self.auto_check,
        ):
            button.configure(state=common_state)
        for index, row in enumerate(self.channel_rows):
            state = tk.NORMAL if connected and configured and index < self.configured_count else tk.DISABLED
            for widget in row.widgets:
                widget.configure(state="readonly" if isinstance(widget, ttk.Combobox)
                                 and state == tk.NORMAL else state)

    def _append_log(self, text: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, f"[{timestamp}] {text}\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def on_close(self) -> None:
        self.disconnect()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    AD9910App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
