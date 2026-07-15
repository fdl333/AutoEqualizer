from __future__ import annotations

import json
import math
import os
import queue
import re
import sys
import threading
import time
import traceback
import ctypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tkinter import (
    BOTH,
    BOTTOM,
    DISABLED,
    END,
    LEFT,
    NORMAL,
    RIGHT,
    TOP,
    Button,
    Canvas,
    DoubleVar,
    Frame,
    Label,
    Listbox,
    Scale,
    StringVar,
    Tk,
    filedialog,
    messagebox,
)

import numpy as np
import sounddevice as sd

try:
    import soundcard as sc
except ImportError:
    sc = None


BANDS = [40, 63, 100, 160, 250, 400, 630, 1000, 1600, 2500, 4000, 6300, 10000, 16000]
TEST_LEVELS_DBFS = list(range(-60, -9, 3))
MAX_BOOST_DB = 12.0
DEFAULT_Q = 1.41
SAMPLE_RATE = 48000
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
APO_CONFIG_DIR = Path(r"C:\Program Files\EqualizerAPO\config")
APO_PRESET_NAME = "auto_hearing_eq.txt"
CRASH_LOG_PATH = APP_DIR / "auto_equalizer_crash.log"
RUNTIME_LOG_PATH = APP_DIR / "auto_equalizer_runtime.log"
STATE_PATH = APP_DIR / "auto_equalizer_state.json"
WINDOW_WIDTH = 900
WINDOW_HEIGHT = 720
GRAPH_WIDTH = 610
GRAPH_HEIGHT = 270
GRAPH_PAD_LEFT = 54
GRAPH_PAD_RIGHT = 24
GRAPH_PAD_TOP = 18
GRAPH_PAD_BOTTOM = 38
APO_WRITE_RETRIES = 5
APO_WRITE_RETRY_DELAY_SECONDS = 0.05
SLIDER_COLUMN_WIDTH = 36
METER_BLOCK_SIZE = 4096
METER_MIN_DB = -72.0
METER_MAX_DB = 0.0
METER_SMOOTHING = 0.7
COINIT_MULTITHREADED = 0x0
RPC_E_CHANGED_MODE = 0x80010106


@dataclass
class HearingTest:
    thresholds: dict[int, float] = field(default_factory=dict)
    active_frequency: int | None = None
    active_level: float | None = None
    running: bool = False


def db_to_amplitude(dbfs: float) -> float:
    return float(10 ** (dbfs / 20.0))


def play_tone(frequency: int, dbfs: float, duration: float = 0.75) -> None:
    samples = int(SAMPLE_RATE * duration)
    t = np.arange(samples, dtype=np.float32) / SAMPLE_RATE
    wave = np.sin(2.0 * math.pi * frequency * t)

    fade_samples = min(int(SAMPLE_RATE * 0.025), samples // 2)
    envelope = np.ones(samples, dtype=np.float32)
    if fade_samples:
        fade = np.linspace(0.0, 1.0, fade_samples, dtype=np.float32)
        envelope[:fade_samples] = fade
        envelope[-fade_samples:] = fade[::-1]

    audio = (wave * envelope * db_to_amplitude(dbfs)).astype(np.float32)
    sd.play(audio, SAMPLE_RATE, blocking=True)


def smooth_gains(raw_gains: list[float]) -> list[float]:
    if len(raw_gains) < 3:
        return raw_gains

    smoothed: list[float] = []
    for index, gain in enumerate(raw_gains):
        if index == 0:
            value = (gain * 0.75) + (raw_gains[index + 1] * 0.25)
        elif index == len(raw_gains) - 1:
            value = (gain * 0.75) + (raw_gains[index - 1] * 0.25)
        else:
            value = (raw_gains[index - 1] * 0.2) + (gain * 0.6) + (raw_gains[index + 1] * 0.2)
        smoothed.append(round(min(MAX_BOOST_DB, max(0.0, value)), 1))
    return smoothed


def thresholds_to_gains(thresholds: dict[int, float]) -> list[float]:
    measured = [thresholds.get(freq) for freq in BANDS]
    present = [level for level in measured if level is not None]
    if not present:
        return [0.0 for _ in BANDS]

    best_threshold = min(present)
    raw_gains = []
    for level in measured:
        if level is None:
            raw_gains.append(0.0)
            continue
        deficit = max(0.0, level - best_threshold)
        raw_gains.append(min(MAX_BOOST_DB, deficit * 0.75))
    return smooth_gains(raw_gains)


def band_edges(frequencies: list[int]) -> list[tuple[float, float]]:
    edges: list[tuple[float, float]] = []
    for index, frequency in enumerate(frequencies):
        if index == 0:
            low = frequency / math.sqrt(frequencies[index + 1] / frequency)
        else:
            low = math.sqrt(frequencies[index - 1] * frequency)

        if index == len(frequencies) - 1:
            high = frequency * math.sqrt(frequency / frequencies[index - 1])
        else:
            high = math.sqrt(frequency * frequencies[index + 1])
        edges.append((low, high))
    return edges


BAND_EDGES = band_edges(BANDS)


def audio_to_band_levels_db(audio: np.ndarray, samplerate: int) -> list[float]:
    if audio.size == 0:
        return [METER_MIN_DB for _ in BANDS]

    mono = np.asarray(audio, dtype=np.float32)
    if mono.ndim == 2:
        mono = mono.mean(axis=1)

    if mono.size < 16:
        return [METER_MIN_DB for _ in BANDS]

    mono = mono - float(np.mean(mono))
    window = np.hanning(mono.size).astype(np.float32)
    spectrum = np.fft.rfft(mono * window)
    magnitudes = np.abs(spectrum) / max(float(np.sum(window)) / 2.0, 1.0)
    freqs = np.fft.rfftfreq(mono.size, d=1.0 / samplerate)

    levels: list[float] = []
    for low, high in BAND_EDGES:
        mask = (freqs >= low) & (freqs < high)
        if not np.any(mask):
            levels.append(METER_MIN_DB)
            continue
        rms = float(np.sqrt(np.mean(np.square(magnitudes[mask]))))
        db = 20.0 * math.log10(max(rms, 1e-8))
        levels.append(round(min(METER_MAX_DB, max(METER_MIN_DB, db)), 1))
    return levels


def initialize_com_for_thread() -> bool:
    result = ctypes.windll.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    if result == 0:
        return True
    if result == RPC_E_CHANGED_MODE:
        return False
    raise OSError(f"CoInitializeEx failed: 0x{result & 0xFFFFFFFF:08x}")


def uninitialize_com_for_thread(initialized: bool) -> None:
    if initialized:
        ctypes.windll.ole32.CoUninitialize()


def apo_preset_text(gains: list[float], thresholds: dict[int, float]) -> str:
    preamp = -round(max(gains), 1) if gains else 0.0
    lines = [
        "# Auto Equalizer hearing-compensation preset",
        f"# Created: {datetime.now().isoformat(timespec='seconds')}",
        "# Import this file in Peace, or include it from Equalizer APO config.txt.",
        "# This is not a medical audiogram.",
        f"Preamp: {preamp:.1f} dB",
    ]

    for index, (frequency, gain) in enumerate(zip(BANDS, gains), start=1):
        lines.append(f"Filter {index}: ON PK Fc {frequency} Hz Gain {gain:.1f} dB Q {DEFAULT_Q:.2f}")

    if thresholds:
        lines.append("")
        lines.append("# Measured thresholds, dBFS")
        for frequency in BANDS:
            if frequency in thresholds:
                lines.append(f"# {frequency} Hz: {thresholds[frequency]:.1f}")

    return "\n".join(lines) + "\n"


class AutoEqualizerApp:
    def __init__(self) -> None:
        self.root = Tk()
        self.root.title("Auto Equalizer")
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.root.resizable(False, False)
        self.root.report_callback_exception = self.report_callback_exception
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.log_runtime("started")

        self.test = HearingTest()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.heard_event = threading.Event()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.status = StringVar(value="Ready. Set your Windows volume to a comfortable normal listening level.")
        self.current = StringVar(value="No test running")
        self.gain_vars = [DoubleVar(value=0.0) for _ in BANDS]
        self.loading_state = False
        self.apo_dirty = False
        self.meter_levels = [METER_MIN_DB for _ in BANDS]
        self.meter_stop_event = threading.Event()
        self.meter_thread: threading.Thread | None = None

        self._build_ui()
        self._bind_keys()
        self.load_state()
        self.start_output_meter()
        self._poll_events()

    def log_runtime(self, message: str) -> None:
        with RUNTIME_LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")

    def on_close(self) -> None:
        self.log_runtime("closed by window")
        self.meter_stop_event.set()
        self.root.destroy()

    def report_callback_exception(self, exc_type: type[BaseException], exc: BaseException, tb: object) -> None:
        with CRASH_LOG_PATH.open("a", encoding="utf-8") as log:
            log.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] Tk callback error\n")
            traceback.print_exception(exc_type, exc, tb, file=log)

        messagebox.showerror(
            "Auto Equalizer error",
            f"Something went wrong.\n\nDetails were saved to:\n{CRASH_LOG_PATH}",
        )

    def _build_ui(self) -> None:
        top = Frame(self.root, padx=14, pady=12)
        top.pack(side=TOP, fill=BOTH, expand=False)

        Label(top, text="Auto Equalizer", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        Label(
            top,
            textvariable=self.status,
            font=("Segoe UI", 10),
            wraplength=980,
            justify=LEFT,
        ).pack(anchor="w", pady=(6, 0))
        Label(top, textvariable=self.current, font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(10, 0))

        buttons = Frame(top)
        buttons.pack(anchor="w", pady=(12, 0))

        self.start_button = Button(buttons, text="Start Hearing Test", command=self.start_test, width=18)
        self.start_button.pack(side=LEFT, padx=(0, 8))
        self.heard_button = Button(buttons, text="I can hear it (Space)", command=self.mark_heard, width=20, state=DISABLED)
        self.heard_button.pack(side=LEFT, padx=(0, 8))
        self.stop_button = Button(buttons, text="Stop", command=self.stop_test, width=10, state=DISABLED)
        self.stop_button.pack(side=LEFT, padx=(0, 8))
        Button(buttons, text="Recalculate EQ", command=self.apply_test_gains, width=14).pack(side=LEFT, padx=(0, 8))
        self.apply_button = Button(buttons, text="Apply To APO", command=self.apply_to_apo, width=14)
        self.apply_button.pack(side=LEFT, padx=(0, 8))
        Button(buttons, text="Export APO Preset", command=self.export_preset, width=18).pack(side=LEFT)

        main = Frame(self.root, padx=14, pady=8)
        main.pack(side=TOP, fill=BOTH, expand=True)

        left = Frame(main)
        left.pack(side=LEFT, fill=BOTH, expand=False)

        self.graph = Canvas(left, bg="#111827", highlightthickness=0, width=GRAPH_WIDTH, height=GRAPH_HEIGHT)
        self.graph.pack(side=TOP, fill=BOTH, expand=False)
        self.graph.bind("<Configure>", lambda _event: self.draw_graph())

        sliders = Frame(left)
        sliders.configure(width=GRAPH_WIDTH, height=266)
        sliders.pack(side=TOP, fill=BOTH, expand=False, pady=(14, 0))
        sliders.pack_propagate(False)

        for index, frequency in enumerate(BANDS):
            column = Frame(sliders)
            x = self._graph_x(index, GRAPH_WIDTH)
            column.place(x=round(x - (SLIDER_COLUMN_WIDTH / 2)), y=0, width=SLIDER_COLUMN_WIDTH, height=266)
            scale = Scale(
                column,
                from_=MAX_BOOST_DB,
                to=-12,
                resolution=0.5,
                orient="vertical",
                variable=self.gain_vars[index],
                length=230,
                width=10,
                sliderlength=14,
                showvalue=False,
                command=lambda _value: self.on_slider_changed(),
            )
            scale.pack()
            Label(column, text=self._freq_label(frequency), font=("Segoe UI", 8)).pack()

        right = Frame(main, padx=12)
        right.pack(side=RIGHT, fill=BOTH, expand=False)
        Label(right, text="Thresholds", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self.threshold_list = Listbox(right, height=18, width=28)
        self.threshold_list.pack(side=TOP, fill=BOTH, expand=True, pady=(8, 0))

        note = (
            "Space records the current tone as heard. If you never hear a tone before the cap, "
            "the app records the cap and moves on."
        )
        Label(right, text=note, wraplength=240, justify=LEFT).pack(side=BOTTOM, anchor="w", pady=(12, 0))

    def _bind_keys(self) -> None:
        self.root.bind("<space>", lambda _event: self.mark_heard())
        self.root.bind("<Escape>", lambda _event: self.stop_test())

    def start_output_meter(self) -> None:
        if sc is None:
            self.status.set("Output meter unavailable: install the soundcard package to enable loopback capture.")
            return

        self.meter_stop_event.clear()
        self.meter_thread = threading.Thread(target=self._run_output_meter, daemon=True)
        self.meter_thread.start()

    def _run_output_meter(self) -> None:
        com_initialized = False
        try:
            com_initialized = initialize_com_for_thread()
            speaker = sc.default_speaker()
            microphone = sc.get_microphone(speaker.name, include_loopback=True)
            self.events.put(("meter_status", f"Output meter listening to {speaker.name}."))

            with microphone.recorder(samplerate=SAMPLE_RATE, channels=2) as recorder:
                while not self.meter_stop_event.is_set():
                    audio = recorder.record(numframes=METER_BLOCK_SIZE)
                    levels = audio_to_band_levels_db(audio, SAMPLE_RATE)
                    self.events.put(("meter", levels))
        except Exception as exc:
            self.events.put(("meter_error", exc))
        finally:
            uninitialize_com_for_thread(com_initialized)

    def load_state(self) -> None:
        if not STATE_PATH.exists():
            self.load_state_from_apo_config()
            return

        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            saved_bands = state.get("bands_hz", [])
            gains = state.get("gains_db", {})
            thresholds = state.get("thresholds_dbfs", {})
            self.loading_state = True
            for index, frequency in enumerate(BANDS):
                self.gain_vars[index].set(float(gains.get(str(frequency), 0.0)))
            self.test.thresholds = {int(freq): float(level) for freq, level in thresholds.items()}
            self.apo_dirty = bool(state.get("apo_dirty", False))
        except (OSError, ValueError, TypeError) as exc:
            self.status.set(f"Could not load saved configuration: {exc}")
            return
        finally:
            self.loading_state = False

        self.update_threshold_list()
        self.draw_graph()
        if saved_bands and saved_bands != BANDS:
            self.apo_dirty = True
            self.save_state()
            self.status.set("Loaded and migrated the saved curve to 14 bands. Review it, then click Apply To APO.")
        elif self.apo_dirty:
            self.status.set("Loaded the last saved curve. It has local changes that are not applied to APO yet.")
        else:
            self.status.set("Loaded the last saved curve.")

    def load_state_from_apo_config(self) -> None:
        config_path = APO_CONFIG_DIR / "config.txt"
        if not config_path.exists():
            return

        try:
            config_text = config_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return

        gains_by_freq: dict[int, float] = {}
        for match in re.finditer(r"Fc\s+(\d+)\s+Hz\s+Gain\s+(-?\d+(?:\.\d+)?)\s+dB", config_text):
            gains_by_freq[int(match.group(1))] = float(match.group(2))

        if not gains_by_freq:
            return

        self.loading_state = True
        for index, frequency in enumerate(BANDS):
            self.gain_vars[index].set(float(gains_by_freq.get(frequency, 0.0)))
        self.loading_state = False
        self.apo_dirty = False
        self.draw_graph()
        self.save_state()
        self.status.set("Loaded the curve currently applied in Equalizer APO.")

    def save_state(self) -> None:
        if self.loading_state:
            return

        gains = [round(var.get(), 1) for var in self.gain_vars]
        state = {
            "saved": datetime.now().isoformat(timespec="seconds"),
            "bands_hz": BANDS,
            "thresholds_dbfs": {str(freq): level for freq, level in self.test.thresholds.items()},
            "gains_db": {str(freq): gain for freq, gain in zip(BANDS, gains)},
            "apo_dirty": self.apo_dirty,
        }
        try:
            STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except OSError as exc:
            self.status.set(f"Could not save configuration: {exc}")

    def start_test(self) -> None:
        if self.test.running:
            return

        if not messagebox.askokcancel(
            "Before starting",
            "Set Windows volume to a comfortable normal listening level.\n\n"
            "The test begins very quiet and increases gradually. Stop immediately if anything feels too loud.",
        ):
            return

        self.test = HearingTest(running=True)
        self.heard_event.clear()
        self.stop_event.clear()
        self._set_test_buttons(True)
        self.update_threshold_list()
        self.status.set("Test running. Press Space as soon as you can hear each tone.")
        self.worker = threading.Thread(target=self._run_test, daemon=True)
        self.worker.start()

    def stop_test(self) -> None:
        if not self.test.running:
            return
        self.stop_event.set()
        self.heard_event.set()
        self.status.set("Stopping test...")

    def mark_heard(self) -> None:
        if self.test.running and self.test.active_frequency is not None:
            self.heard_event.set()

    def _run_test(self) -> None:
        try:
            for frequency in BANDS:
                if self.stop_event.is_set():
                    break

                heard_level = None
                for level in TEST_LEVELS_DBFS:
                    if self.stop_event.is_set():
                        break

                    self.test.active_frequency = frequency
                    self.test.active_level = float(level)
                    self.heard_event.clear()
                    self.events.put(("current", (frequency, level)))
                    play_tone(frequency, level)

                    deadline = time.monotonic() + 0.35
                    while time.monotonic() < deadline:
                        if self.heard_event.is_set() or self.stop_event.is_set():
                            break
                        time.sleep(0.01)

                    if self.heard_event.is_set() and not self.stop_event.is_set():
                        heard_level = float(level)
                        break

                if heard_level is None and not self.stop_event.is_set():
                    heard_level = float(TEST_LEVELS_DBFS[-1])

                if heard_level is not None:
                    self.test.thresholds[frequency] = heard_level
                    self.events.put(("threshold", (frequency, heard_level)))

            self.events.put(("done", None))
        except Exception as exc:
            self.events.put(("error", exc))

    def _poll_events(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break

            if kind == "current":
                frequency, level = payload
                self.current.set(f"Testing {self._freq_label(frequency)} at {level:.0f} dBFS")
            elif kind == "threshold":
                self.update_threshold_list()
            elif kind == "meter":
                incoming = payload
                self.meter_levels = [
                    round((previous * METER_SMOOTHING) + (current * (1.0 - METER_SMOOTHING)), 1)
                    for previous, current in zip(self.meter_levels, incoming)
                ]
                self.draw_graph()
            elif kind == "meter_status":
                if not self.test.running:
                    self.status.set(str(payload))
            elif kind == "meter_error":
                if not self.test.running:
                    self.status.set(f"Output meter unavailable: {payload}")
            elif kind == "done":
                was_stopped = self.stop_event.is_set()
                self.test.running = False
                self.test.active_frequency = None
                self.test.active_level = None
                self._set_test_buttons(False)
                self.current.set("No test running")
                self.apply_test_gains()
                if was_stopped:
                    self.status.set("Test stopped. The curve uses only the thresholds measured so far.")
                else:
                    self.status.set("Test complete. Review the curve, then click Apply To APO when ready.")
            elif kind == "error":
                self.test.running = False
                self._set_test_buttons(False)
                self.current.set("No test running")
                messagebox.showerror("Audio error", str(payload))
                self.status.set("Audio playback failed. Check that your output device is available.")

        self.root.after(60, self._poll_events)

    def _set_test_buttons(self, running: bool) -> None:
        self.start_button.configure(state=DISABLED if running else NORMAL)
        self.heard_button.configure(state=NORMAL if running else DISABLED)
        self.stop_button.configure(state=NORMAL if running else DISABLED)
        self.apply_button.configure(state=DISABLED if running else NORMAL)

    def apply_test_gains(self) -> None:
        gains = thresholds_to_gains(self.test.thresholds)
        self.loading_state = True
        for var, gain in zip(self.gain_vars, gains):
            var.set(gain)
        self.loading_state = False
        self.apo_dirty = True
        self.draw_graph()
        self.save_state()

    def on_slider_changed(self) -> None:
        self.draw_graph()
        if not self.loading_state:
            self.apo_dirty = True
            self.save_state()
            self.status.set("Curve changed locally. Click Apply To APO when you want to hear it.")

    def export_preset(self) -> None:
        gains = [round(var.get(), 1) for var in self.gain_vars]
        initial_dir = APO_CONFIG_DIR if APO_CONFIG_DIR.exists() else Path.cwd()
        filename = filedialog.asksaveasfilename(
            title="Export Equalizer APO preset",
            initialdir=str(initial_dir),
            initialfile=APO_PRESET_NAME,
            defaultextension=".txt",
            filetypes=[("Equalizer APO preset", "*.txt"), ("All files", "*.*")],
        )
        if not filename:
            return

        path = Path(filename)
        measurement_path = path.with_suffix(".measurements.json")
        measurement = {
            "created": datetime.now().isoformat(timespec="seconds"),
            "bands_hz": BANDS,
            "thresholds_dbfs": {str(freq): level for freq, level in self.test.thresholds.items()},
            "gains_db": {str(freq): gain for freq, gain in zip(BANDS, gains)},
            "max_boost_db": MAX_BOOST_DB,
        }

        try:
            path.write_text(apo_preset_text(gains, self.test.thresholds), encoding="utf-8")
            measurement_path.write_text(json.dumps(measurement, indent=2), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror(
                "Could not export preset",
                f"Windows could not save the preset here:\n{path}\n\n"
                "Try saving to your Documents folder, then import that file in Peace.\n\n"
                f"Details: {exc}",
            )
            return

        messagebox.showinfo(
            "Preset exported",
            f"Saved:\n{path}\n\nAlso saved measurement data:\n{measurement_path}",
        )

    def apply_to_apo(self) -> None:
        if not APO_CONFIG_DIR.exists():
            messagebox.showerror(
                "Equalizer APO not found",
                f"I could not find the Equalizer APO config folder:\n{APO_CONFIG_DIR}",
            )
            return

        config_path = APO_CONFIG_DIR / "config.txt"
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = APO_CONFIG_DIR / f"config.before-auto-eq.{timestamp}.txt"

        if not messagebox.askokcancel(
            "Apply To Equalizer APO",
            f"This will write the current curve to:\n{config_path}\n\n"
            f"Your current config.txt will be backed up as:\n{backup_path.name}\n\n"
            "Slider changes after this will stay local until you click Apply To APO again.",
        ):
            return

        try:
            if config_path.exists():
                backup_path.write_text(config_path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            gains = [round(var.get(), 1) for var in self.gain_vars]
            self.atomic_write_text(config_path, apo_preset_text(gains, self.test.thresholds))
        except OSError as exc:
            messagebox.showerror(
                "Could not apply to APO",
                "Windows blocked writing to Equalizer APO's config folder.\n\n"
                "Run the app as Administrator, or use Export APO Preset and import it in Peace.\n\n"
                f"Details: {exc}",
            )
            return

        self.apo_dirty = False
        self.save_state()
        self.status.set(f"Applied to APO at {datetime.now().strftime('%H:%M:%S')}.")
        messagebox.showinfo(
            "Applied to Equalizer APO",
            f"Saved current curve to:\n{config_path}\n\nBackup created:\n{backup_path}",
        )

    @staticmethod
    def atomic_write_text(path: Path, text: str) -> None:
        temp_path = path.with_name(f"{path.name}.tmp")
        last_error: OSError | None = None
        for _attempt in range(APO_WRITE_RETRIES):
            try:
                temp_path.write_text(text, encoding="utf-8")
                os.replace(temp_path, path)
                return
            except OSError as exc:
                last_error = exc
                time.sleep(APO_WRITE_RETRY_DELAY_SECONDS)

        if last_error is not None:
            raise last_error

    def update_threshold_list(self) -> None:
        self.threshold_list.delete(0, END)
        for frequency in BANDS:
            if frequency in self.test.thresholds:
                self.threshold_list.insert(END, f"{self._freq_label(frequency):>7}: {self.test.thresholds[frequency]:5.1f} dBFS")
            else:
                self.threshold_list.insert(END, f"{self._freq_label(frequency):>7}: not tested")

    def draw_graph(self) -> None:
        canvas = self.graph
        canvas.delete("all")
        width = GRAPH_WIDTH
        height = GRAPH_HEIGHT
        plot_h = height - GRAPH_PAD_TOP - GRAPH_PAD_BOTTOM
        plot_bottom = height - GRAPH_PAD_BOTTOM

        for db in range(-12, 13, 6):
            y = GRAPH_PAD_TOP + ((MAX_BOOST_DB - db) / (MAX_BOOST_DB + 12)) * plot_h
            canvas.create_line(GRAPH_PAD_LEFT, y, width - GRAPH_PAD_RIGHT, y, fill="#263244")
            canvas.create_text(10, y, anchor="w", fill="#cbd5e1", text=f"{db:+d} dB", font=("Segoe UI", 8))

        points = []
        for index, frequency in enumerate(BANDS):
            x = self._graph_x(index, width)
            gain = self.gain_vars[index].get()
            y = GRAPH_PAD_TOP + ((MAX_BOOST_DB - gain) / (MAX_BOOST_DB + 12)) * plot_h
            points.append((x, y, gain, frequency))

            canvas.create_line(x, GRAPH_PAD_TOP, x, height - GRAPH_PAD_BOTTOM, fill="#1f2937")
            canvas.create_text(x, height - 18, fill="#cbd5e1", text=self._freq_label(frequency), font=("Segoe UI", 8))

        self.draw_meter_bars(canvas, points, plot_bottom)

        zero_y = GRAPH_PAD_TOP + ((MAX_BOOST_DB - 0) / (MAX_BOOST_DB + 12)) * plot_h
        canvas.create_line(GRAPH_PAD_LEFT, zero_y, width - GRAPH_PAD_RIGHT, zero_y, fill="#94a3b8", width=2)

        if len(points) > 1:
            line_coords = [(x, y) for x, y, _gain, _frequency in points]
            canvas.create_line(*[coord for point in line_coords for coord in point], fill="#38bdf8", width=3, smooth=True)

        for x, y, gain, _frequency in points:
            canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill="#f8fafc", outline="#38bdf8", width=2)
            canvas.create_text(x, y - 16, fill="#e0f2fe", text=f"{gain:+.1f}", font=("Segoe UI", 8, "bold"))

    def draw_meter_bars(self, canvas: Canvas, points: list[tuple[float, float, float, int]], plot_bottom: int) -> None:
        if not points:
            return

        bar_width = max(10, int((GRAPH_WIDTH - GRAPH_PAD_LEFT - GRAPH_PAD_RIGHT) / (len(BANDS) * 2.4)))
        for (x, _y, _gain, _frequency), level_db in zip(points, self.meter_levels):
            normalized = (level_db - METER_MIN_DB) / (METER_MAX_DB - METER_MIN_DB)
            normalized = min(1.0, max(0.0, normalized))
            bar_top = plot_bottom - (normalized * (plot_bottom - GRAPH_PAD_TOP))
            color = "#22c55e" if level_db < -12 else "#f59e0b"
            canvas.create_rectangle(
                x - (bar_width / 2),
                bar_top,
                x + (bar_width / 2),
                plot_bottom,
                fill=color,
                outline="",
                stipple="gray50",
            )

    @staticmethod
    def _graph_x(index: int, width: int) -> float:
        plot_w = width - GRAPH_PAD_LEFT - GRAPH_PAD_RIGHT
        return GRAPH_PAD_LEFT + (index / (len(BANDS) - 1)) * plot_w

    @staticmethod
    def _freq_label(frequency: int) -> str:
        if frequency >= 1000:
            value = frequency / 1000
            if value.is_integer():
                return f"{int(value)}k"
            return f"{value:g}k"
        return str(frequency)

    def run(self) -> None:
        self.draw_graph()
        self.root.mainloop()


if __name__ == "__main__":
    AutoEqualizerApp().run()
