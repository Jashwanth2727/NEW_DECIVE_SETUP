#!/usr/bin/env python3
import asyncio
import builtins
import csv
import json
import shutil
import socket
import time
import urllib.request
import urllib.error
import logging
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from bleak import BleakClient, BleakScanner
from bleak.args.bluez import BlueZNotifyArgs
import pexpect
from dbus_fast.aio import MessageBus
from dbus_fast import BusType, Variant, Message, MessageType
import board
import busio
import neopixel_spi as neopixel

try:
    import board
    import busio
    import neopixel_spi as neopixel
    HAS_LEDS = True
except ImportError:
    HAS_LEDS = False
    print("[warn] rpi_ws281x not installed — LED indicators disabled")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BACKEND_URL          = "https://prod.o-health.in/api/v2/vitals/update"
CLEARVITALS_POLL_URL = "https://prod.o-health.in/api/v2/vitals/latestVitalsForKiosk"
STATUS_URL           = "https://prod.o-health.in/api/v2/vitals/updateStatus"

KIOSK_ID             = socket.gethostname()
SPOOL_DIR            = Path.home() / "vitals_spool"
VIDEO_BASE_DIR       = Path.home() / "videos"
ALT_VIDEO_ROOT       = Path("/home/rpi23/VIDEOS")
LOG_DIR              = Path.home() / "vitals_logs"
CONSOLE_LOG_PATH     = LOG_DIR / "vitals_kiosk_console.log"
SESSION_CSV_PATH     = LOG_DIR / "vitals_kiosk_sessions.csv"
DOWNLOADS_DIR        = Path("/home/rpi23/Downloads")
DOWNLOADS_LOG_PATH   = DOWNLOADS_DIR / "logsuser.txt"
DOWNLOADS_CSV_PATH   = DOWNLOADS_DIR / "logs.csv"

PUSH_TIMEOUT                 = 10
PUSH_RETRIES                 = 2
PUSH_RETRY_DELAY             = 5
SCAN_TIMEOUT                 = 8
CONNECT_TIMEOUT              = 30
BP_CONNECT_TIMEOUT           = 15
POLL_INTERVAL_SECS           = 15
POLL_TIMEOUT_SECS            = 8
BP_MEASURE_TIMEOUT           = 90
SPO2_MEASURE_TIMEOUT         = 90
PATIENT_IDLE_PRINT_EVERY     = 60
TEMP_WAIT_AFTER_SPO2         = 0
PARALLEL_DISCOVERY_SECS      = 40
SENSOR_CONNECT_ATTEMPTS      = 3
CAMERA_DEVICE                = "/dev/video0"
CAMERA_RECORD_SECS           = 5
GLUC_ON_SECS                 = 10
LOCKOUT_SECS                 = 180
POST_PRIMARY_FALLBACK_SCAN_SECS = 10
BLE_DEBUG_LOG                = False

BP_ALLOWED_MAC   = "FF:FF:11:9C:90:1A"
BP_NAME_HINTS    = ["JPD", "BPM", "TRACKY"]
BP_NOTIFY        = "0000fff1-0000-1000-8000-00805f9b34fb"
NOTIFY_ARGS      = BlueZNotifyArgs(use_start_notify=True)

SPO2_ALLOWED_MAC   = "10:22:33:41:5F:1B"
SPO2_NAME_HINTS    = ["OXIMETER", "MY OXIMETER", "YK-80B"]
SPO2_NOTIFY        = "cdeacb81-5235-4c07-8846-93a37ee6b86d"
SPO2_STABLE_COUNT  = 6
SPO2_MIN_VALID     = 85    # reject readings below this before adding to stable buffer

NULL_BP   = {"sys": None, "dia": None, "pul": None}
NULL_SPO2 = {"spo2": None, "pr": None, "pi": None}
NULL_TEMP = {
    "ambient_c": None,
    "object_c": None,
    "object_c_raw": None,
    "display_source": None,
    "samples": None,
}

TEMP_SERVICE_UUID        = "0000fff0-0000-1000-8000-00805f9b34fb"
TEMP_NOTIFY_UUID         = "0000fff3-0000-1000-8000-00805f9b34fb"
TEMP_ALLOWED_MAC         = "FF:FF:11:70:71:2A"
TEMP_NAME_HINTS          = ["THERMOMETER", "MY THERMOMETER"]
TEMP_BLE_CONNECT_TIMEOUT = 60
TEMP_BLE_MEASURE_TIMEOUT = 60

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
_console_log_fh   = open(CONSOLE_LOG_PATH,   "a", buffering=1, encoding="utf-8")
_downloads_log_fh = open(DOWNLOADS_LOG_PATH, "a", buffering=1, encoding="utf-8")

_builtin_print = builtins.print

def _logging_print(*args, **kwargs):
    try:
        text = " ".join(str(a) for a in args)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for line in text.splitlines() or [""]:
            entry = f"[{ts}] {line}\n"
            _builtin_print(f"[{ts}] {line}", **{k: v for k, v in kwargs.items() if k != "end"})
            _console_log_fh.write(entry)
            _downloads_log_fh.write(entry)
    except Exception:
        _builtin_print(*args, **kwargs)

print = _logging_print

def log_session_divider():
    sep = "=" * 70
    try:
        _console_log_fh.write(f"\n{sep}\n")
        _downloads_log_fh.write(f"\n{sep}\n")
    except Exception:
        pass

CSV_FIELDNAMES = [
    "patient_seq", "session_start", "session_end", "duration_secs", "trigger",
    "bp_sys", "bp_dia", "bp_pul",
    "spo2_pct", "spo2_pr", "spo2_pi",
    "temp_ambient_f", "temp_object_f", "temp_display_source",
    "pushed_ok",
]

def _ensure_csv_header():
    if not SESSION_CSV_PATH.exists():
        with open(SESSION_CSV_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_FIELDNAMES)

def _ensure_downloads_csv_header():
    if not DOWNLOADS_CSV_PATH.exists():
        with open(DOWNLOADS_CSV_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_FIELDNAMES)

def log_session_csv_row(patient_num, started_at, ended_at, trigger_kind,
                         bp_result, spo2_result, temp_result, pushed_ok):
    _ensure_csv_header()
    _ensure_downloads_csv_header()
    duration = (ended_at - started_at).total_seconds()
    row = [
        patient_num,
        started_at.strftime("%Y-%m-%d %H:%M:%S"),
        ended_at.strftime("%Y-%m-%d %H:%M:%S"),
        f"{duration:.1f}",
        trigger_kind,
        bp_result.get("sys") if bp_result else "",
        bp_result.get("dia") if bp_result else "",
        bp_result.get("pul") if bp_result else "",
        spo2_result.get("spo2") if spo2_result else "",
        spo2_result.get("pr") if spo2_result else "",
        spo2_result.get("pi") if spo2_result else "",
        temp_result.get("ambient_c") if temp_result else "",
        temp_result.get("object_c") if temp_result else "",
        temp_result.get("display_source") if temp_result else "",
        "OK" if pushed_ok else "FAILED/SPOOLED",
    ]
    try:
        with open(SESSION_CSV_PATH, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)
    except Exception as e:
        _builtin_print(f"  [log] CSV row write failed: {type(e).__name__}: {e}")
    try:
        with open(DOWNLOADS_CSV_PATH, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)
    except Exception as e:
        _builtin_print(f"  [log] Downloads CSV row write failed: {type(e).__name__}: {e}")

# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------

_bg_tasks: set = set()

def _track(coro):
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task

_ble_adapter_lock  = asyncio.Lock()

async def _quick_disconnect(client):
    try:
        async with _ble_adapter_lock:
            await asyncio.wait_for(client.disconnect(), timeout=1.5)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Status update
# ---------------------------------------------------------------------------

_status_state = {
    "bp":   {"start": False, "stop": False},
    "spo2": {"start": False, "stop": False},
    "temp": {"start": False, "stop": False},
}

def _reset_status_state():
    for sensor in _status_state:
        _status_state[sensor]["start"] = False
        _status_state[sensor]["stop"]  = False

def update_status(sensor: str, event: str):
    if sensor not in _status_state or event not in ("start", "stop"):
        return
    opposite = "stop" if event == "start" else "start"
    _status_state[sensor][event] = True
    _status_state[sensor][opposite] = False
    payload = {
        "kiosk_id": KIOSK_ID,
        "bp":   dict(_status_state["bp"]),
        "spo2": dict(_status_state["spo2"]),
        "temp": dict(_status_state["temp"]),
    }
    print(f"  [Status] updateStatus -> {sensor}:{event}")
    _track(_fire_status(payload))

async def _fire_status(payload: dict):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        STATUS_URL, data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        await asyncio.to_thread(lambda: urllib.request.urlopen(req, timeout=5))
    except Exception as e:
        print(f"  [Status] POST failed (non-fatal): {type(e).__name__}: {e}")

# ---------------------------------------------------------------------------
# Non-blocking HTTP
# ---------------------------------------------------------------------------

def _push_once(record) -> bool:
    body = json.dumps(record).encode("utf-8")
    req  = urllib.request.Request(
        BACKEND_URL, data=body,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=PUSH_TIMEOUT) as resp:
            print(f"  Backend response: HTTP {resp.status}")
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"  Push failed: {type(e).__name__}: {e}")
        return False

async def _push_once_async(record) -> bool:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _push_once, record)

def _check_clearvitals_sync() -> bool:
    body = json.dumps({"kiosk_id": KIOSK_ID}).encode("utf-8")
    req = urllib.request.Request(
        CLEARVITALS_POLL_URL, data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=POLL_TIMEOUT_SECS) as resp:
            raw = resp.read().decode("utf-8").strip()
            if not raw:
                return False
            data = json.loads(raw)
            return data.get("data") is None
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"  [poll] clearvitals HTTP {e.code} (non-fatal)")
        return False
    except json.JSONDecodeError:
        return False
    except Exception as e:
        print(f"  [poll] clearvitals check error (non-fatal): {type(e).__name__}: {e}")
        return False

async def _check_clearvitals():
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _check_clearvitals_sync)

# ---------------------------------------------------------------------------
# LED controller
# ---------------------------------------------------------------------------

LED_FREQ_HZ    = 800000
LED_INVERT     = False
LED_BRIGHTNESS = 128

# Strip A — GPIO 10 (DMA 5): LOGO + GLUC
LED_COUNT_A    = 4
LED_PIN_A      = 10
LED_DMA_A      = 5
LED_LOGO       = [0, 1]
LED_GLUC       = [2, 3]

# Strip B — SPI1 GPIO 20: SPO2 + MIC + BP
LED_COUNT_B    = 6
LED_PIN_B      = 20
LED_DMA_B      = 8
LED_BP         = [0, 1]
LED_MIC        = [2, 3]
LED_SPO2       = [4, 5]

def _c(r, g, b):
    return (r << 16) | (g << 8) | b

GREEN = _c(0, 255, 0)
WHITE = _c(255, 255, 255)
RED   = _c(255, 0, 0)
OFF   = _c(0, 0, 0)

BREATH_FADE_STEPS = 25
BREATH_FADE_DELAY = 0.04
BREATH_HOLD_SECS  = 0.5

class LEDState(Enum):
    OFF       = "off"
    WAIT_BP   = "wait_bp"
    CAPTURE   = "capture"
    POST_FLOW = "post_flow"
    ERROR     = "error"

class LEDController:
    def __init__(self):
        self.pixels_a = None
        self.pixels_b = None
        self.state = LEDState.OFF
        self._state_started = time.monotonic()
        self._stop = False
        self._bp_active = False
        self._spo2_active = False
        self._spo2_in_wait = False
        self._gluc_active = False

        if not HAS_LEDS:
            return

        try:
            self.spi0 = board.SPI()
            self.spi1 = busio.SPI(
                clock=board.D21,
                MOSI=board.D20,
                MISO=board.D19
            )

            self.pixels_a = neopixel.NeoPixel_SPI(
                spi=self.spi0,
                n=LED_COUNT_A,
                pixel_order=neopixel.GRB,
                brightness=LED_BRIGHTNESS / 255.0,
                auto_write=False
            )

            self.pixels_b = neopixel.NeoPixel_SPI(
                spi=self.spi1,
                n=LED_COUNT_B,
                pixel_order=neopixel.GRB,
                brightness=LED_BRIGHTNESS / 255.0,
                auto_write=False
            )

            self._all_off()
            self._set_pixels_a(LED_LOGO, WHITE)
            self._show()

            print("  [LED] SPI0 GPIO10 initialized")
            print("  [LED] SPI1 GPIO20 initialized")

        except Exception as e:
            print(f"  [warn] LED init failed: {type(e).__name__}: {e}")
            self.pixels_a = None
            self.pixels_b = None

    @staticmethod
    def _rgb(color):
        return (
            (color >> 16) & 0xFF,
            (color >> 8) & 0xFF,
            color & 0xFF
        )

    def _all_off(self):
        if self.pixels_a:
            self.pixels_a.fill((0, 0, 0))
            self.pixels_a.show()

        if self.pixels_b:
            self.pixels_b.fill((0, 0, 0))
            self.pixels_b.show()

    def _set_pixels_a(self, leds, color):
        if not self.pixels_a:
            return

        rgb = self._rgb(color)
        for i in leds:
            self.pixels_a[i] = rgb

    def _set_pixels_b(self, leds, color):
        if not self.pixels_b:
            return

        rgb = self._rgb(color)
        for i in leds:
            self.pixels_b[i] = rgb

    def _show(self):
        if self.pixels_a:
            self.pixels_a.show()

        if self.pixels_b:
            self.pixels_b.show()

    @staticmethod
    def _scale(color, factor):
        r = (color >> 16) & 0xFF
        g = (color >> 8) & 0xFF
        b = color & 0xFF
        return _c(int(r * factor), int(g * factor), int(b * factor))

    @staticmethod
    def _breath_factor(elapsed):
        cycle_len = (
            2 * (BREATH_FADE_STEPS * BREATH_FADE_DELAY)
            + 2 * BREATH_HOLD_SECS
        )
        t = elapsed % cycle_len
        fade_dur = BREATH_FADE_STEPS * BREATH_FADE_DELAY

        if t < fade_dur:
            return t / fade_dur

        t -= fade_dur
        if t < BREATH_HOLD_SECS:
            return 1.0

        t -= BREATH_HOLD_SECS
        if t < fade_dur:
            return 1.0 - (t / fade_dur)

        return 0.0

    def set_state(self, new_state: LEDState):
        if self.state != new_state:
            print(f"  [LED] {self.state.value} -> {new_state.value}")
            self.state = new_state
            self._state_started = time.monotonic()

            if new_state in (
                LEDState.OFF,
                LEDState.ERROR,
                LEDState.POST_FLOW
            ):
                self._bp_active = False
                self._spo2_active = False
                self._gluc_active = False

            if new_state in (
                LEDState.CAPTURE,
                LEDState.POST_FLOW,
                LEDState.OFF,
                LEDState.ERROR
            ):
                self._spo2_in_wait = False

    def set_bp_active(self, active: bool):
        self._bp_active = active

    def set_spo2_active(self, active: bool):
        self._spo2_active = active

    def set_spo2_in_wait(self, active: bool):
        self._spo2_in_wait = active

    def set_gluc_active(self, active: bool):
        self._gluc_active = active

    def _render(self):
        if not self.pixels_a and not self.pixels_b:
            return

        elapsed = time.monotonic() - self._state_started

        if self.pixels_a:
            self.pixels_a.fill((0, 0, 0))

        if self.pixels_b:
            self.pixels_b.fill((0, 0, 0))

        self._set_pixels_a(LED_LOGO, WHITE)

        if self.state == LEDState.WAIT_BP:
            factor = self._breath_factor(elapsed)
            self._set_pixels_b(LED_BP, self._scale(GREEN, factor))

            if self._spo2_in_wait:
                self._set_pixels_b(
                    LED_SPO2,
                    self._scale(GREEN, factor)
                )

        elif self.state == LEDState.CAPTURE:
            factor = self._breath_factor(elapsed)

            if self._bp_active:
                self._set_pixels_b(
                    LED_BP,
                    self._scale(GREEN, factor)
                )

            if self._spo2_active:
                self._set_pixels_b(
                    LED_SPO2,
                    self._scale(GREEN, factor)
                )

            if self._gluc_active:
                self._set_pixels_a(LED_GLUC, GREEN)

        elif self.state == LEDState.POST_FLOW:
            if elapsed < LOCKOUT_SECS:
                self._set_pixels_b(LED_MIC, GREEN)

        elif self.state == LEDState.ERROR:
            self._set_pixels_b(
                LED_BP + LED_SPO2 + LED_MIC,
                RED
            )
            self._set_pixels_a(LED_GLUC, RED)

        self._show()

    async def run(self):
        if not self.pixels_a and not self.pixels_b:
            return

        try:
            while not self._stop:
                self._render()
                await asyncio.sleep(BREATH_FADE_DELAY)
        finally:
            self._all_off()

    def shutdown(self):
        self._stop = True
        self._all_off()

# ---------------------------------------------------------------------------
# BLE helpers
# ---------------------------------------------------------------------------

def _normalize_mac(addr):
    return (addr or "").strip().upper()

def _is_bp_device(dev):
    if not dev:
        return False
    return _normalize_mac(dev.address) == _normalize_mac(BP_ALLOWED_MAC)

def _is_spo2_device(dev):
    if not dev:
        return False
    return _normalize_mac(dev.address) == _normalize_mac(SPO2_ALLOWED_MAC)

async def scan_known_devices(timeout=SCAN_TIMEOUT):
    found_bp   = None
    found_spo2 = None
    try:
        devices = await BleakScanner.discover(timeout=timeout, return_adv=False)
    except Exception as e:
        print(f"  [BLE] Scan failed: {type(e).__name__}: {e}")
        return None, None
    for dev in devices:
        if BLE_DEBUG_LOG:
            print(f"  [BLE seen] {dev.name or '(unnamed)'} @ {dev.address}")
        if _is_bp_device(dev):
            found_bp = dev
            print(f"  [BLE match] BP candidate: {dev.name or '(unnamed)'} @ {dev.address}")
        if _is_spo2_device(dev):
            found_spo2 = dev
            print(f"  [BLE match] SpO2 candidate: {dev.name or '(unnamed)'} @ {dev.address}")
    return found_bp, found_spo2

async def scan_for_specific_device(kind: str, timeout: int, leds=None):
    print(f"  [{kind}] Scanning up to {timeout}s for connection...")
    try:
        if leds:
            if kind == "BP":
                leds.set_bp_active(True)
            elif kind == "SpO2":
                leds.set_spo2_active(True)
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            remaining = timeout - (time.monotonic() - start)
            pass_timeout = min(5, max(2, remaining))
            bp_dev, spo2_dev = await scan_known_devices(timeout=pass_timeout)
            if kind == "BP" and bp_dev:
                print(f"  [BP] Found during secondary scan: {bp_dev.name or '(unnamed)'} @ {bp_dev.address}")
                return bp_dev
            if kind == "SpO2" and spo2_dev:
                print(f"  [SpO2] Found during secondary scan: {spo2_dev.name or '(unnamed)'} @ {spo2_dev.address}")
                return spo2_dev
            await asyncio.sleep(0.5)
        print(f"  [{kind}] Not found within {timeout}s — skipping {kind}.")
        return None
    finally:
        if leds:
            if kind == "BP":
                leds.set_bp_active(False)
            elif kind == "SpO2":
                leds.set_spo2_active(False)

async def wait_for_patient_device(leds=None):
    print("\n>>> WAITING FOR PATIENT — power on BP cuff OR SpO2 to begin <<<\n")
    if leds:
        leds.set_spo2_in_wait(False)
    last_print = time.time()
    started = time.time()
    while True:
        bp_dev, spo2_dev = await scan_known_devices(timeout=SCAN_TIMEOUT)
        if leds:
            leds.set_spo2_in_wait(spo2_dev is not None)
        if bp_dev or spo2_dev:
            if bp_dev:
                print(f"  ✓ BP detected: {bp_dev.name or '(unnamed)'} @ {bp_dev.address}")
            if spo2_dev:
                print(f"  ✓ SpO2 detected: {spo2_dev.name or '(unnamed)'} @ {spo2_dev.address}")
            if bp_dev and spo2_dev:
                print("  ✓ Both devices found — will run BP + SpO2 in parallel")
            return bp_dev, spo2_dev
        now = time.time()
        if now - last_print >= PATIENT_IDLE_PRINT_EVERY:
            print(f"  ... still waiting ({int((now - started) / 60)} min elapsed)")
            last_print = now
        await asyncio.sleep(2)

# ---------------------------------------------------------------------------
# BP Session
# ---------------------------------------------------------------------------

class BPSession:
    def __init__(self, leds=None):
        self.result = None
        self.done = asyncio.Event()
        self.leds = leds
        self._debug_frames = 0

    def on_notify(self, _, data):
        if self.done.is_set():
            return
        if self._debug_frames < 20:
            print(f"  [BP raw] {bytes(data).hex()}")
            self._debug_frames += 1
        if (len(data) == 8
                and data[0] == 0xFD and data[1] == 0xFD and data[2] == 0xFC
                and data[-2:] == b'\x0D\x0A'):
            self.result = {"sys": data[3], "dia": data[4], "pul": data[5]}
            print(f"  ✓ BP: {data[3]}/{data[4]} mmHg, pulse {data[5]}")
            update_status("bp", "stop")
            self.done.set()

    async def run(self, device):
        if not device:
            print("  [BP] No BP device available — skipping BP.")
            return None

        client = BleakClient(device, timeout=CONNECT_TIMEOUT)
        all_notify_tasks = []
        watcher_task = None
        max_retries = 5

        async def _subscribe_with_retries():
            attempt = 0
            while attempt < max_retries and not self.done.is_set():
                attempt += 1
                async with _ble_adapter_lock:
                    t = asyncio.create_task(client.start_notify(BP_NOTIFY, self.on_notify, bluez=NOTIFY_ARGS))
                    all_notify_tasks.append(t)
                    done_set, _ = await asyncio.wait([t], timeout=3.0)

                if self.done.is_set():
                    return
                if done_set:
                    exc = t.exception()
                    if exc:
                        print(f"  [BP] start_notify attempt {attempt} failed: {type(exc).__name__}: {exc}")
                        continue
                    print(f"  [BP] Notify subscribed ✓ (attempt {attempt})")
                    return
                else:
                    print(f"  [BP] Notify subscription pending (attempt {attempt}, continuing)")
                    try:
                        await t
                        print(f"  [BP] Notify subscribed ✓ (attempt {attempt}, late)")
                        return
                    except Exception as e:
                        if self.done.is_set():
                            return
                        print(f"  [BP] start_notify attempt {attempt} failed late: {type(e).__name__}: {e}")
                        continue

            if not self.done.is_set() and attempt >= max_retries:
                print(f"  [BP] Gave up resubscribing after {max_retries} attempts.")

        try:
            if self.leds:
                self.leds.set_bp_active(True)
            update_status("bp", "start")
            async with _ble_adapter_lock:
                await client.connect()
            print("  [BP] Connected ✓")
            await asyncio.sleep(0.5)
            watcher_task = asyncio.create_task(_subscribe_with_retries())

            if self.done.is_set():
                print("  [BP] Value captured during subscription — done.")
            else:
                print(f"  [BP] Measurement timer started ({BP_MEASURE_TIMEOUT}s)")
                print(f"  Press START on the BP cuff. ({BP_MEASURE_TIMEOUT}s to complete)")
                try:
                    await asyncio.wait_for(self.done.wait(), timeout=BP_MEASURE_TIMEOUT)
                except asyncio.TimeoutError:
                    print(f"  [BP] Measurement timed out after {BP_MEASURE_TIMEOUT}s — skipping BP.")
        except Exception as e:
            print(f"  [BP] Connection/measurement failed: {type(e).__name__}: {e}")
        finally:
            if watcher_task and not watcher_task.done():
                watcher_task.cancel()
            for t in all_notify_tasks:
                if not t.done():
                    t.cancel()
                else:
                    try:
                        t.result()
                    except Exception:
                        pass
            if self.result is None:
                update_status("bp", "stop")
            _track(_quick_disconnect(client))
            if self.leds:
                self.leds.set_bp_active(False)
        return self.result

# ---------------------------------------------------------------------------
# SpO2 — bluetoothctl connection + BlueZ D-Bus notifications
# ---------------------------------------------------------------------------

def _spo2_valid(spo2, pr):
    return SPO2_MIN_VALID <= spo2 <= 100 and 30 <= pr <= 250


def _close_btctl(child):
    if child is None:
        return
    try:
        child.sendline(f"disconnect {SPO2_ALLOWED_MAC}")
        child.sendline("quit")
        child.close()
    except Exception:
        pass


def _bluetoothctl_connect_spo2():
    print("  [SpO2] Connecting through bluetoothctl...")
    child = pexpect.spawn("bluetoothctl", encoding="utf-8", timeout=10)
    try:
        time.sleep(0.5)
        child.sendline("scan on")
        time.sleep(5)
        child.sendline(f"trust {SPO2_ALLOWED_MAC}")
        time.sleep(0.5)
        child.sendline(f"connect {SPO2_ALLOWED_MAC}")
        result = child.expect(
            [r"ServicesResolved: yes", r"Connection successful",
             pexpect.TIMEOUT, pexpect.EOF],
            timeout=30,
        )
        if result in (0, 1):
            print("  [SpO2] Connected via bluetoothctl ✓")
            return child
        print("  [SpO2] bluetoothctl connection timed out or closed.")
    except Exception as e:
        print(f"  [SpO2] bluetoothctl error: {type(e).__name__}: {e}")
    _close_btctl(child)
    return None


async def _find_spo2_notify_characteristic(bus):
    intro = await bus.introspect("org.bluez", "/")
    obj = bus.get_proxy_object("org.bluez", "/", intro)
    manager = obj.get_interface("org.freedesktop.DBus.ObjectManager")
    managed = await manager.call_get_managed_objects()
    suffix = SPO2_ALLOWED_MAC.upper().replace(":", "_")
    for path, interfaces in managed.items():
        if suffix not in path.upper():
            continue
        characteristic = interfaces.get("org.bluez.GattCharacteristic1")
        if not characteristic:
            continue
        flags = characteristic.get("Flags", [])
        if isinstance(flags, Variant):
            flags = flags.value
        if "notify" in flags or "indicate" in flags:
            return path
    return None


async def capture_spo2_new():
    readings = []
    done = asyncio.Event()
    child = None
    bus = None

    async with _ble_adapter_lock:
        for attempt in range(1, SENSOR_CONNECT_ATTEMPTS + 1):
            print(f"  [SpO2] Connection attempt {attempt}/{SENSOR_CONNECT_ATTEMPTS}")
            child = await asyncio.to_thread(_bluetoothctl_connect_spo2)
            if child is not None:
                break
            if attempt < SENSOR_CONNECT_ATTEMPTS:
                await asyncio.sleep(2)
        if child is None:
            print("  [SpO2] All connection attempts failed.")
            return None
        try:
            await asyncio.sleep(2)
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            path = await _find_spo2_notify_characteristic(bus)
            if path is None:
                print("  [SpO2] Notify characteristic not found.")
                return None

            print(f"  [SpO2] Notify characteristic: {path}")

            def on_signal(message):
                if (message.message_type != MessageType.SIGNAL
                        or message.member != "PropertiesChanged"
                        or message.path != path):
                    return
                changed = message.body[1]
                if not isinstance(changed, dict) or "Value" not in changed:
                    return
                value = changed["Value"]
                data = bytes(value.value if isinstance(value, Variant) else value)
                if len(data) < 4 or data[0] != 0x81:
                    return
                spo2 = data[1]
                pr = data[2]
                pi = round(data[3] / 10.0, 1)
                if not _spo2_valid(spo2, pr):
                    print(f"  [SpO2] Invalid reading ignored: SpO2={spo2} PR={pr}")
                    return
                if done.is_set():
                    return
                readings.append({"spo2": spo2, "pr": pr, "pi": pi})
                print(f"  [SpO2] Reading #{len(readings)}: SpO2={spo2}% PR={pr} PI={pi:.1f}%")
                if len(readings) >= SPO2_STABLE_COUNT:
                    update_status("spo2", "stop")
                    done.set()

            bus.add_message_handler(on_signal)
            await bus.call(Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch",
                signature="s",
                body=[
                    "type='signal',sender='org.bluez',"
                    "interface='org.freedesktop.DBus.Properties',"
                    "member='PropertiesChanged',"
                    f"path='{path}'"
                ],
            ))

            char_intro = await bus.introspect("org.bluez", path)
            char_obj = bus.get_proxy_object("org.bluez", path, char_intro)
            char_gatt = char_obj.get_interface("org.bluez.GattCharacteristic1")
            await char_gatt.call_start_notify()
            update_status("spo2", "start")
            print(f"  [SpO2] Insert finger; collecting {SPO2_STABLE_COUNT} valid readings.")
            try:
                await asyncio.wait_for(done.wait(), timeout=SPO2_MEASURE_TIMEOUT)
            except asyncio.TimeoutError:
                print(f"  [SpO2] Timeout; collected {len(readings)} valid readings.")
                update_status("spo2", "stop")
            finally:
                try:
                    await char_gatt.call_stop_notify()
                except Exception:
                    pass
        except Exception as e:
            print(f"  [SpO2] D-Bus error: {type(e).__name__}: {e}")
            update_status("spo2", "stop")
        finally:
            if bus is not None:
                try:
                    bus.disconnect()
                except Exception:
                    pass
            await asyncio.to_thread(_close_btctl, child)

    if len(readings) >= SPO2_STABLE_COUNT:
        result = readings[SPO2_STABLE_COUNT - 1]
        print(f"  ✓ SpO2 (6th reading): {result['spo2']}% / PR {result['pr']} / PI {result['pi']:.1f}")
        return result
    return None


class SpO2Session:
    def __init__(self, warmup_frames=1, leds=None):
        self.leds = leds

    async def run(self, device=None):
        if self.leds:
            self.leds.set_spo2_active(True)
        try:
            return await capture_spo2_new()
        finally:
            if self.leds:
                self.leds.set_spo2_active(False)


# ---------------------------------------------------------------------------
# Temperature sensor
# ---------------------------------------------------------------------------

class TempSensor:
    def __init__(self):
        self._celsius = None
        self._done = asyncio.Event()

    def _on_notify(self, _handle, data: bytearray):
        if self._done.is_set():
            return
        print(f"  [Temp BLE packet] {' '.join(f'{b:02X}' for b in data)}  len={len(data)}")
        if len(data) == 5 and data[0] == 0xAA and data[1] == 0x33:
            try:
                raw_value = int.from_bytes(data[2:4], byteorder="big", signed=False)
                self._celsius = raw_value / 100.0
                print(f"  [Temp BLE raw] {' '.join(f'{b:02X}' for b in data)}")
                self._done.set()
            except Exception as e:
                print(f"  [Temp BLE parse error] {e}")

    async def _find_device(self):
        deadline = time.monotonic() + PARALLEL_DISCOVERY_SECS
        attempt = 0

        while time.monotonic() < deadline:
            attempt += 1
            remaining = deadline - time.monotonic()
            print(f"  [Temp] Search attempt {attempt}; {remaining:.0f}s left")

            try:
                device = await BleakScanner.find_device_by_address(
                    TEMP_ALLOWED_MAC, timeout=min(5, remaining)
                )
                if device:
                    print(f"  [Temp] Found by MAC: {device.address}")
                    return device
            except Exception as exc:
                print(f"  [Temp] MAC search error: {type(exc).__name__}: {exc}")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            try:
                devices = await BleakScanner.discover(timeout=min(5, remaining))
                for device in devices:
                    name = (device.name or "").upper()
                    if (device.address.upper() == TEMP_ALLOWED_MAC.upper()
                            or any(hint in name for hint in TEMP_NAME_HINTS)):
                        print(
                            f"  [Temp] Found: {device.name or device.address} "
                            f"@ {device.address}"
                        )
                        return device
            except Exception as exc:
                print(f"  [Temp] Name search error: {type(exc).__name__}: {exc}")

        print(f"  [Temp] Not found within {PARALLEL_DISCOVERY_SECS}s.")
        return None

    async def read_average(self):
        self._celsius = None
        self._done.clear()

        device = await self._find_device()
        if not device:
            print("  [Temp] Thermometer not found — press measure to wake BLE.")
            return None

        for attempt in range(1, SENSOR_CONNECT_ATTEMPTS + 1):
            client = BleakClient(device, timeout=TEMP_BLE_CONNECT_TIMEOUT)
            try:
                print(f"  [Temp] Connection attempt {attempt}/{SENSOR_CONNECT_ATTEMPTS}")
                async with _ble_adapter_lock:
                    await client.connect()

                print("  [Temp] Connected; subscribing to FFF3...")
                await client.start_notify(
                    TEMP_NOTIFY_UUID,
                    self._on_notify,
                    bluez=BlueZNotifyArgs(use_start_notify=True),
                )
                update_status("temp", "start")
                print(
                    f"  [Temp] Take a forehead measurement now "
                    f"({TEMP_BLE_MEASURE_TIMEOUT}s)."
                )
                try:
                    await asyncio.wait_for(
                        self._done.wait(), timeout=TEMP_BLE_MEASURE_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    print("  [Temp] Connected, but no measurement packet arrived.")
                finally:
                    try:
                        await client.stop_notify(TEMP_NOTIFY_UUID)
                    except Exception:
                        pass
                break
            except Exception as exc:
                print(f"  [Temp] Attempt {attempt} failed: {type(exc).__name__}: {exc}")
                if attempt < SENSOR_CONNECT_ATTEMPTS:
                    await asyncio.sleep(2)
            finally:
                try:
                    if client.is_connected:
                        async with _ble_adapter_lock:
                            await client.disconnect()
                except Exception as exc:
                    print(f"  [Temp] Disconnect warning: {exc}")

        update_status("temp", "stop")
        if self._celsius is None:
            return None

        celsius = self._celsius
        fahrenheit = round(celsius * 9.0 / 5.0 + 32.0, 2)
        print(f"  Temp: {celsius:.2f}C ({fahrenheit:.2f}F) [ble_direct]")
        return {
            "ambient_c": None,
            "object_c": fahrenheit,
            "object_c_raw": fahrenheit,
            "display_source": "ble_direct",
            "samples": 1,
        }

# ---------------------------------------------------------------------------
# Backend push / spool
# ---------------------------------------------------------------------------

async def push_to_backend_with_retry(record):
    total = 1 + PUSH_RETRIES
    for attempt in range(1, total + 1):
        print(f"\n  [push attempt {attempt}/{total}] -> {BACKEND_URL}")
        if await _push_once_async(record):
            print("  Pushed.")
            return True
        if attempt < total:
            print(f"  Will retry in {PUSH_RETRY_DELAY}s...")
            await asyncio.sleep(PUSH_RETRY_DELAY)
    print(f"  All {total} push attempts failed.")
    return False

async def push_partial(record_base: dict, bp, spo2, temp, partial: bool):
    snapshot = {
        **record_base,
        "bp": bp if bp is not None else NULL_BP,
        "spo2": spo2 if spo2 is not None else NULL_SPO2,
        "temp": temp if temp is not None else NULL_TEMP,
        "partial": partial,
    }
    label = "partial" if partial else "final"
    print(f"\n  [SSE {label} push] bp={'captured' if bp else 'null'}  "
          f"spo2={'captured' if spo2 else 'null'}  temp={'captured' if temp else 'null'}")
    if partial:
        _track(_push_once_async(snapshot))
        return None
    else:
        pushed = await push_to_backend_with_retry(snapshot)
        if not pushed:
            spool_to_disk(snapshot)
        return pushed

def spool_to_disk(record):
    SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    path = SPOOL_DIR / f"vitals_{int(time.time())}.json"
    path.write_text(json.dumps(record, indent=2))
    print(f"  Spooled: {path}")

async def drain_spool():
    if not SPOOL_DIR.exists():
        return
    loop = asyncio.get_running_loop()
    files = sorted(SPOOL_DIR.glob("vitals_*.json"))
    for f in files:
        try:
            raw = f.read_text().strip()
            if not raw:
                print(f"  [spool] Removing empty spool file {f.name}")
                f.unlink()
                continue
            record = json.loads(raw)
            ok = await loop.run_in_executor(None, _push_once, record)
            if ok:
                f.unlink()
                print(f"  [spool] Pushed and removed {f.name}")
            else:
                break
        except json.JSONDecodeError:
            print(f"  [spool] Removing corrupt JSON spool file {f.name}")
            try:
                f.unlink()
            except Exception:
                pass
        except Exception as e:
            print(f"  [spool] Skipping {f.name}: {e}")

async def poll_for_clearvitals(skip_event: asyncio.Event) -> None:
    print(f"  [poll] Polling latestVitalsForKiosk every {POLL_INTERVAL_SECS}s — will exit lockout early if data becomes null (clearvitals called)")
    while not skip_event.is_set():
        await asyncio.sleep(POLL_INTERVAL_SECS)
        if skip_event.is_set():
            return
        triggered = await _check_clearvitals()
        if triggered:
            print("  [poll] clearvitals signal received — ending lockout early")
            skip_event.set()
            return

# ---------------------------------------------------------------------------
# Camera
# ---------------------------------------------------------------------------

async def record_video(patient_num: int, leds):
    ts = datetime.now()
    date_str = ts.strftime("%Y-%m-%d")
    time_str = ts.strftime("%H_%M_%S_%d%m%Y")
    filename = f"{time_str}_patient{patient_num}.mp4"
    main_folder = VIDEO_BASE_DIR / date_str
    main_folder.mkdir(parents=True, exist_ok=True)
    alt_folder = ALT_VIDEO_ROOT / date_str
    alt_folder.mkdir(parents=True, exist_ok=True)
    out_path = main_folder / filename
    alt_path = alt_folder / filename

    cmd = [
        "ffmpeg", "-y", "-f", "v4l2",
        "-input_format", "yuyv422",
        "-video_size", "1280x720", "-framerate", "15",
        "-i", CAMERA_DEVICE,
        "-t", str(CAMERA_RECORD_SECS), "-vcodec", "libx264", "-preset", "ultrafast",
        str(out_path),
    ]

    print(f"\n  [Camera] Recording {CAMERA_RECORD_SECS}s -> {out_path}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=CAMERA_RECORD_SECS + 15)
        if proc.returncode == 0:
            print(f"  [Camera] ✓ Saved: {out_path}")
            try:
                shutil.copy2(out_path, alt_path)
                print(f"  [Camera] ✓ Copied: {alt_path}")
            except Exception as copy_err:
                print(f"  [Camera] Copy failed: {type(copy_err).__name__}: {copy_err}")
        else:
            err = stderr.decode(errors="replace").strip().splitlines()
            print(f"  [Camera] ffmpeg error (rc={proc.returncode}): {err[-1] if err else '(no output)'}")
    except Exception as e:
        print(f"  [Camera] Error: {type(e).__name__}: {e}")

# ---------------------------------------------------------------------------
# Patient capture
# ---------------------------------------------------------------------------

async def run_patient_capture(patient_num, bp_dev, spo2_dev, temp_sensor, leds):
    session_started_at = datetime.now()
    trigger_kind = "BP" if bp_dev else "SpO2"

    print(f"\n{'#' * 60}")
    print(f"# PATIENT #{patient_num} @ {session_started_at.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"# Trigger: {trigger_kind}  |  bp_dev={'yes' if bp_dev else 'no'}  spo2_dev={'yes' if spo2_dev else 'no'}")
    print("#" * 60)

    record_base = {
        "kiosk_id": KIOSK_ID,
        "patient_seq": patient_num,
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    leds.set_state(LEDState.CAPTURE)
    leds.set_bp_active(False)
    leds.set_spo2_active(False)

    print("\n=== BP + SpO2 (parallel) ===")

    bp_result = None
    spo2_result = None

    async def _run_bp(device):
        return await BPSession(leds=leds).run(device)

    async def _scan_then_bp():
        found = await scan_for_specific_device("BP", CONNECT_TIMEOUT, leds=leds)
        if found:
            return await BPSession(leds=leds).run(found)
        print("  [Flow] BP device not found during background scan — skipping.")
        return None

    async def _run_spo2(device):
        await asyncio.sleep(6)  # give BP a clean window to connect before SpO2 acquires adapter
        return await SpO2Session(leds=leds).run(device)

    async def _scan_then_spo2():
        found = await scan_for_specific_device("SpO2", PARALLEL_DISCOVERY_SECS, leds=leds)
        if found:
            return await SpO2Session(leds=leds).run(found)
        print("  [Flow] SpO2 device not found during background scan — skipping.")
        return None

    bp_coro = _run_bp(bp_dev) if bp_dev else _scan_then_bp()
    spo2_coro = _run_spo2(spo2_dev) if spo2_dev else _scan_then_spo2()

    async def _run_temp():
        # Give BP and SpO2 time to begin connecting, without waiting for
        # either measurement to finish.
        await asyncio.sleep(TEMP_WAIT_AFTER_SPO2)
        print("\\n=== Temperature (parallel) ===")
        leds.set_gluc_active(True)
        try:
            return await temp_sensor.read_average()
        finally:
            leds.set_gluc_active(False)

    bp_task = asyncio.create_task(bp_coro)
    spo2_task = asyncio.create_task(spo2_coro)
    temp_task = asyncio.create_task(_run_temp())

    temp_result = None
    pending = {bp_task, spo2_task, temp_task}

    while pending:
        done_now, pending = await asyncio.wait(
            pending, return_when=asyncio.FIRST_COMPLETED
        )

        for task in done_now:
            try:
                result = task.result()
            except Exception as exc:
                print(f"  [Flow] Task raised: {type(exc).__name__}: {exc}")
                result = None

            if task is bp_task:
                bp_result = result
                if bp_result:
                    print("  [Flow] BP done — partial push")
                    await push_partial(
                        record_base, bp=bp_result, spo2=spo2_result,
                        temp=temp_result, partial=True
                    )
                else:
                    print("  [Flow] BP produced no result.")

            elif task is spo2_task:
                spo2_result = result
                if spo2_result:
                    print("  [Flow] SpO2 done — partial push")
                    await push_partial(
                        record_base, bp=bp_result, spo2=spo2_result,
                        temp=temp_result, partial=True
                    )
                else:
                    print("  [Flow] SpO2 produced no result.")

            elif task is temp_task:
                temp_result = result
                if temp_result:
                    print("  [Flow] Temp done — partial push")
                    await push_partial(
                        record_base, bp=bp_result, spo2=spo2_result,
                        temp=temp_result, partial=True
                    )
                else:
                    print("  [Flow] Temp produced no result.")

    print("\n=== Camera ===")

    camera_task = asyncio.create_task(record_video(patient_num, leds))
    await camera_task

    print("\n=== Final Push ===")
    record_final = {
        **record_base,
        "bp":   bp_result   if bp_result   else NULL_BP,
        "spo2": spo2_result if spo2_result else NULL_SPO2,
        "temp": temp_result if temp_result else NULL_TEMP,
    }
    print("\n=== RECORD ===")
    print(json.dumps(record_final, indent=2))
    pushed_ok = await push_partial(
        record_base, bp=bp_result, spo2=spo2_result,
        temp=temp_result, partial=False
    )

    session_ended_at = datetime.now()
    log_session_csv_row(
        patient_num, session_started_at, session_ended_at, trigger_kind,
        bp_result, spo2_result, temp_result, pushed_ok
    )

    leds.set_state(LEDState.POST_FLOW)
    print(f"\n  [Lockout] Post-flow lockout for up to {LOCKOUT_SECS}s (or until clearvitals signal). BP light resumes after.")

    skip_event = asyncio.Event()
    poll_task = asyncio.create_task(poll_for_clearvitals(skip_event))
    lockout_task = asyncio.create_task(asyncio.sleep(LOCKOUT_SECS))

    done, pending = await asyncio.wait([poll_task, lockout_task], return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    leds.set_state(LEDState.WAIT_BP)
    log_session_divider()
    print("  [Lockout] Ended. Ready for next patient.\n")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    leds = LEDController()
    temp_sensor = TempSensor()
    patient_num = 0

    led_task = asyncio.create_task(leds.run())
    leds.set_state(LEDState.WAIT_BP)

    print("\n" + "=" * 60)
    print("  O-Health Vitals Kiosk — rpi23")
    print("=" * 60)

    await drain_spool()

    try:
        while True:
            _reset_status_state()
            bp_dev, spo2_dev = await wait_for_patient_device(leds)
            patient_num += 1
            await run_patient_capture(patient_num, bp_dev, spo2_dev, temp_sensor, leds)
    except KeyboardInterrupt:
        print("\n  Shutdown requested.")
    finally:
        leds.shutdown()
        led_task.cancel()
        try:
            await led_task
        except asyncio.CancelledError:
            pass
        _console_log_fh.close()
        _downloads_log_fh.close()
        print("  Goodbye.")

if __name__ == "__main__":
    asyncio.run(main())
