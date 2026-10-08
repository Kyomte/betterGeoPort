"""
Quitting must put devices back on their real location: iOS keeps a simulated
location until it is cleared or the device restarts, so a process that just
dies leaves the device stuck there.

Runs main.py's real quit handlers in a child process (the device reset is
faked to leave a marker file) and quits it the ways people do on this OS:
  macOS / Linux: SIGTERM, SIGHUP (Terminal window closed), SIGINT (Ctrl+C)
  Windows:       closing the console window
  both:          the macOS app's launcher going away (--watch-pid)
"""
import os
import sys
import time
import signal
import tempfile
import subprocess

IS_WINDOWS = sys.platform == "win32"


def child(marker, main_args):
    sys.argv = [sys.argv[0], *main_args]       # main.py parses CLI args at import
    import main

    def fake_shutdown(timeout=4):
        time.sleep(0.5)                         # resetting devices takes a moment
        with open(marker, "w") as f:
            f.write("released")

    main.manager.shutdown = fake_shutdown
    main.manager.sessions = lambda: ["device"]
    main.install_quit_handlers(main.args.watch_pid)
    hwnd = 0
    if IS_WINDOWS:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    with open(marker + ".ready", "w") as f:
        f.write(str(hwnd))
    while True:
        time.sleep(0.2)


def start_child(*main_args, new_console=False):
    marker = os.path.join(tempfile.mkdtemp(), "released")
    kwargs = {}
    if new_console:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7                      # SW_SHOWMINNOACTIVE
        kwargs = dict(creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=si)
    python = sys.executable
    if IS_WINDOWS and sys._base_executable != sys.executable:
        # A venv's python.exe only starts the real interpreter and relays its exit
        # code, and closing the console makes Windows end it with 0xC000013A once
        # main.py is done. Run the interpreter itself, as multiprocessing does.
        python = sys._base_executable
        kwargs["env"] = {**os.environ, "__PYVENV_LAUNCHER__": sys.executable}
    proc = subprocess.Popen([python, os.path.abspath(__file__), "--child", marker,
                             *main_args], cwd=os.path.dirname(os.path.abspath(__file__)),
                            **kwargs)
    for _ in range(600):
        if os.path.exists(marker + ".ready"):
            with open(marker + ".ready") as f:
                return proc, marker, int(f.read() or 0)
        if proc.poll() is not None:
            sys.exit(f"FAIL: child exited early (rc={proc.returncode})")
        time.sleep(0.1)
    proc.kill()
    sys.exit("FAIL: child never got ready")


def expect_released(proc, marker, how):
    try:
        rc = proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        sys.exit(f"FAIL: still running 15 s after {how}")
    assert os.path.exists(marker), f"{how}: quit without putting devices back (rc={rc})"
    assert rc == 0, f"{how}: exit code {rc}"
    print(f"PASS: {how} puts devices back on their real location, then quits")


def main():
    if IS_WINDOWS:
        import ctypes
        proc, marker, hwnd = start_child(new_console=True)
        assert hwnd, "child has no console window"
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)     # WM_CLOSE, i.e. the X button
        expect_released(proc, marker, "closing the console window")
    else:
        for sig, how in ((signal.SIGTERM, "SIGTERM (logout, kill)"),
                         (signal.SIGHUP, "SIGHUP (Terminal window closed)"),
                         (signal.SIGINT, "SIGINT (Ctrl+C)")):
            proc, marker, _ = start_child()
            proc.send_signal(sig)
            expect_released(proc, marker, how)

    launcher = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    proc, marker, _ = start_child("--watch-pid", str(launcher.pid))
    time.sleep(1.5)
    assert proc.poll() is None, "quit while the launcher was still running"
    launcher.kill()
    launcher.wait()
    expect_released(proc, marker, "the app's launcher ending (--watch-pid)")

    print("\nQUIT HANDLING VERIFIED")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--child"]:
        child(sys.argv[2], sys.argv[3:])
    else:
        main()
