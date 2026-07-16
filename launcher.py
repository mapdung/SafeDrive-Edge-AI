import os
import time
import signal
import socket
import subprocess
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

try:
    import snap7
    from snap7.util import get_bool
except ImportError:
    print("Missing python-snap7. Please install it in .venv first.")
    raise


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

MAIN_SCRIPT = os.path.join(BASE_DIR, "main.py")

PYTHON_EXE = os.path.join(BASE_DIR, ".venv", "Scripts", "python.exe")
PYTHONW_EXE = os.path.join(BASE_DIR, ".venv", "Scripts", "pythonw.exe")

LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LAUNCHER_DEBUG_LOG = os.path.join(LOG_DIR, "launcher_debug.log")
MAIN_LOG = os.path.join(LOG_DIR, "main_from_launcher.log")

DESKTOP_DIR = os.path.join(os.path.expanduser("~"), "Desktop")
if not os.path.isdir(DESKTOP_DIR):
    DESKTOP_DIR = os.path.join(os.environ.get("PUBLIC", r"C:\Users\Public"), "Desktop")

DESKTOP_LOG_DIR = os.path.join(DESKTOP_DIR, "SafeDrive_RUNTIME_LOGS")
os.makedirs(DESKTOP_LOG_DIR, exist_ok=True)

DESKTOP_LAUNCHER_LOG = os.path.join(DESKTOP_LOG_DIR, "launcher_debug.log")
DESKTOP_MAIN_LOG = os.path.join(DESKTOP_LOG_DIR, "main_from_launcher.log")

PLC_IP = os.getenv("PLC_IP", "192.168.150.103")
PLC_RACK = int(os.getenv("PLC_RACK", "0"))
PLC_SLOT = int(os.getenv("PLC_SLOT", "1"))

# Production default: S7-1500 start bit DB511.DBX0.5.
# Override in .env if another crane uses another DB bit.
START_DB_NUMBER = int(os.getenv("START_DB_NUMBER", "511"))
START_BYTE = int(os.getenv("START_BYTE", "0"))
START_BIT = int(os.getenv("START_BIT", "5"))

POLL_SEC = float(os.getenv("PLC_POLL_SEC", "0.5"))
RECONNECT_SEC = float(os.getenv("PLC_RECONNECT_SEC", "2.0"))

START_ON_STABLE_SEC = float(os.getenv("START_ON_STABLE_SEC", "1.5"))
START_OFF_STABLE_SEC = float(os.getenv("START_OFF_STABLE_SEC", "1.0"))

MAIN_RESTART_COOLDOWN_SEC = float(os.getenv("MAIN_RESTART_COOLDOWN_SEC", "20.0"))
MAIN_MAX_CRASH = int(os.getenv("MAIN_MAX_CRASH", "3"))

LOCK_HOST = "127.0.0.1"
LOCK_PORT = int(os.getenv("LAUNCHER_LOCK_PORT", "58741"))

MOCK_PLC = os.getenv("MOCK_PLC", "false").strip().lower() in ("1", "true", "yes", "on")
HEADLESS = os.getenv("HEADLESS", "0")

client: Any = None
main_proc: subprocess.Popen[Any] | None = None
launcher_lock: socket.socket | None = None

last_raw_state: bool | None = None
last_raw_change_ts = 0.0
stable_state = False

last_debug_print_ts = 0.0
last_main_start_ts = 0.0
crash_count = 0
restart_locked_until_plc_off = False


def append_text(path: str, text: str) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass


def log(msg: str) -> None:
    text = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(text, flush=True)
    append_text(LAUNCHER_DEBUG_LOG, text)
    append_text(DESKTOP_LAUNCHER_LOG, text)


def acquire_single_instance_lock() -> bool:
    global launcher_lock

    try:
        launcher_lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        launcher_lock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        launcher_lock.bind((LOCK_HOST, LOCK_PORT))
        launcher_lock.listen(1)
        return True
    except OSError:
        log("[LAUNCHER] Another launcher is already running.")
        return False


def cleanup_client() -> None:
    global client

    try:
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass

            try:
                client.destroy()
            except Exception:
                pass
    finally:
        client = None


def ensure_client() -> bool:
    global client

    if MOCK_PLC:
        return True

    if client is not None:
        try:
            if client.get_connected():
                return True
        except Exception:
            pass

    cleanup_client()

    try:
        client = snap7.client.Client()
        log(
            "[LAUNCHER] Connecting PLC "
            f"ip={PLC_IP} rack={PLC_RACK} slot={PLC_SLOT} "
            f"start=DB{START_DB_NUMBER}.DBX{START_BYTE}.{START_BIT}"
        )
        client.connect(PLC_IP, PLC_RACK, PLC_SLOT)
        ok = bool(client.get_connected())
        log(f"[LAUNCHER] PLC connected={ok}")
        return ok
    except Exception as e:
        log(f"[LAUNCHER] PLC connect error: {repr(e)}")
        cleanup_client()
        stop_main()
        return False


def read_start_bit_raw() -> bool:
    global last_debug_print_ts

    if MOCK_PLC:
        now = time.time()
        if now - last_debug_print_ts >= 5.0:
            log("[LAUNCHER] MOCK_PLC=true -> start_enable=True")
            last_debug_print_ts = now
        return True

    if client is None:
        raise RuntimeError("PLC client is None")

    data = client.db_read(START_DB_NUMBER, START_BYTE, 1)
    raw_value = int(data[0])
    start_enable = bool(get_bool(data, 0, START_BIT))

    now = time.time()
    if now - last_debug_print_ts >= 2.0:
        log(
            "[LAUNCHER] PLC READ "
            f"DB{START_DB_NUMBER}.DBB{START_BYTE}=0x{raw_value:02X} "
            f"bits={raw_value:08b} "
            f"DBX{START_BYTE}.{START_BIT}={start_enable}"
        )
        last_debug_print_ts = now

    return start_enable


def debounce_start_bit(raw_state: bool) -> bool:
    global last_raw_state
    global last_raw_change_ts
    global stable_state

    now = time.time()

    if last_raw_state is None:
        last_raw_state = raw_state
        last_raw_change_ts = now
        stable_state = False
        return stable_state

    if raw_state != last_raw_state:
        last_raw_state = raw_state
        last_raw_change_ts = now
        log(f"[LAUNCHER] PLC raw changed -> {raw_state}")
        return stable_state

    stable_time = now - last_raw_change_ts

    if raw_state is True and stable_state is False and stable_time >= START_ON_STABLE_SEC:
        stable_state = True
        log(f"[LAUNCHER] PLC stable ON after {stable_time:.1f}s")
    elif raw_state is False and stable_state is True and stable_time >= START_OFF_STABLE_SEC:
        stable_state = False
        log(f"[LAUNCHER] PLC stable OFF after {stable_time:.1f}s")

    return stable_state


def is_main_running() -> bool:
    global main_proc

    if main_proc is None:
        return False

    return main_proc.poll() is None


def get_runner_python() -> str:
    if os.path.exists(PYTHONW_EXE):
        return PYTHONW_EXE

    if os.path.exists(PYTHON_EXE):
        return PYTHON_EXE

    return "python"


def mirror_main_log_to_desktop() -> None:
    try:
        if os.path.exists(MAIN_LOG):
            with open(MAIN_LOG, "r", encoding="utf-8", errors="ignore") as src:
                data = src.read()
            with open(DESKTOP_MAIN_LOG, "w", encoding="utf-8", errors="ignore") as dst:
                dst.write(data)
    except Exception:
        pass


def start_main() -> None:
    global main_proc
    global last_main_start_ts

    if is_main_running():
        return

    python_exe = get_runner_python()

    if not os.path.exists(MAIN_SCRIPT):
        log(f"[LAUNCHER] ERROR: main.py not found: {MAIN_SCRIPT}")
        return

    now = time.time()
    remain = MAIN_RESTART_COOLDOWN_SEC - (now - last_main_start_ts)

    if last_main_start_ts > 0 and remain > 0:
        log(f"[LAUNCHER] Restart cooldown active: wait {remain:.1f}s")
        return

    log("[LAUNCHER] Starting main.py")
    log(f"[LAUNCHER] MAIN_SCRIPT={MAIN_SCRIPT}")
    log(f"[LAUNCHER] PYTHON_RUNNER={python_exe}")
    log(f"[LAUNCHER] MAIN_LOG={MAIN_LOG}")
    log(f"[LAUNCHER] DESKTOP_LOG_DIR={DESKTOP_LOG_DIR}")

    env = os.environ.copy()
    env["HEADLESS"] = HEADLESS
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW

    try:
        for path in (MAIN_LOG, DESKTOP_MAIN_LOG):
            with open(path, "a", encoding="utf-8", buffering=1) as f:
                f.write("\n\n")
                f.write("=" * 80 + "\n")
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} START main.py\n")
                f.write("=" * 80 + "\n")

        main_log_handle = open(MAIN_LOG, "a", encoding="utf-8", buffering=1)

        main_proc = subprocess.Popen(
            [python_exe, MAIN_SCRIPT],
            cwd=BASE_DIR,
            env=env,
            stdout=main_log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )

        last_main_start_ts = time.time()
        log(f"[LAUNCHER] main.py pid={main_proc.pid}")

    except Exception as e:
        log(f"[LAUNCHER] Failed to start main.py: {repr(e)}")
        main_proc = None


def handle_main_exit_if_needed(start_enable: bool) -> None:
    global main_proc
    global crash_count
    global restart_locked_until_plc_off

    if main_proc is None:
        return

    code = main_proc.poll()

    if code is None:
        return

    log(f"[LAUNCHER] main.py exited with code={code}")
    main_proc = None
    mirror_main_log_to_desktop()

    if start_enable:
        crash_count += 1
        log(f"[LAUNCHER] main.py crash count while PLC ON = {crash_count}/{MAIN_MAX_CRASH}")

        if crash_count >= MAIN_MAX_CRASH:
            restart_locked_until_plc_off = True
            log("[LAUNCHER] Restart locked because main.py crashed too many times.")
            log("[LAUNCHER] Turn PLC start bit OFF then ON again to reset restart lock.")
            log(f"[LAUNCHER] Check Desktop log folder: {DESKTOP_LOG_DIR}")


def stop_main() -> None:
    global main_proc

    if not is_main_running():
        main_proc = None
        return

    log("[LAUNCHER] Stopping main.py")

    try:
        if main_proc is not None:
            if os.name == "nt":
                main_proc.terminate()
            else:
                main_proc.send_signal(signal.SIGTERM)

            main_proc.wait(timeout=5)
    except Exception as e:
        log(f"[LAUNCHER] Graceful stop failed: {repr(e)}")
        try:
            if main_proc is not None:
                main_proc.kill()
        except Exception:
            pass
    finally:
        main_proc = None
        mirror_main_log_to_desktop()


def reset_restart_lock_after_plc_off() -> None:
    global crash_count
    global restart_locked_until_plc_off

    if crash_count != 0 or restart_locked_until_plc_off:
        log("[LAUNCHER] PLC OFF -> reset crash counter and restart lock.")

    crash_count = 0
    restart_locked_until_plc_off = False


def main() -> None:
    log("[LAUNCHER] Started")
    log(f"[LAUNCHER] BASE_DIR={BASE_DIR}")
    log(f"[LAUNCHER] PLC_IP={PLC_IP}")
    log(f"[LAUNCHER] PLC_RACK={PLC_RACK}")
    log(f"[LAUNCHER] PLC_SLOT={PLC_SLOT}")
    log(f"[LAUNCHER] START_BIT=DB{START_DB_NUMBER}.DBX{START_BYTE}.{START_BIT}")
    log(f"[LAUNCHER] MOCK_PLC={MOCK_PLC}")
    log(f"[LAUNCHER] START_ON_STABLE_SEC={START_ON_STABLE_SEC}")
    log(f"[LAUNCHER] MAIN_RESTART_COOLDOWN_SEC={MAIN_RESTART_COOLDOWN_SEC}")
    log(f"[LAUNCHER] MAIN_MAX_CRASH={MAIN_MAX_CRASH}")
    log(f"[LAUNCHER] DESKTOP_LOG_DIR={DESKTOP_LOG_DIR}")

    consecutive_failures = 0
    last_stable_print: bool | None = None

    while True:
        try:
            if not ensure_client():
                backoff = min(RECONNECT_SEC * (2 ** consecutive_failures), 60.0)
                consecutive_failures += 1
                log(f"[LAUNCHER] Reconnect backoff={backoff:.1f}s")
                time.sleep(backoff)
                continue

            consecutive_failures = 0

            raw_start = read_start_bit_raw()
            start_enable = debounce_start_bit(raw_start)

            if start_enable != last_stable_print:
                log(f"[LAUNCHER] start_enable stable -> {start_enable}")
                last_stable_print = start_enable

            handle_main_exit_if_needed(start_enable)

            if start_enable:
                if restart_locked_until_plc_off:
                    time.sleep(POLL_SEC)
                    continue

                if not is_main_running():
                    start_main()
            else:
                stop_main()
                reset_restart_lock_after_plc_off()

        except Exception as e:
            log(f"[LAUNCHER] Loop error: {repr(e)}")
            stop_main()
            cleanup_client()
            time.sleep(RECONNECT_SEC)

        time.sleep(POLL_SEC)


if __name__ == "__main__":
    if not acquire_single_instance_lock():
        raise SystemExit(0)

    try:
        main()
    except KeyboardInterrupt:
        log("[LAUNCHER] Exit by user")
    finally:
        stop_main()
        cleanup_client()
