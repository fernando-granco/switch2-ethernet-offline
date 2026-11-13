# SPDX-License-Identifier: GPL-2.0-or-later
"""Control window (tkinter). It runs exactly what the CLI runs:
windows.setup/restore for the adapter and services.OfflineNet for the link."""
from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

from . import DEFAULT_ADAPTER, __version__, console_settings, windows
from .services import OfflineNet

TITLE = "Switch 2 Offline Ethernet"
GREEN, AMBER, RED, GREY = "#1a9c4a", "#c98a00", "#c0392b", "#8a8a8a"
ROWS = ("Cable", "PC IP", "Firewall", "Server", "Isolation", "Console", "Activity")


def relaunch_as_admin(args) -> None:
    """Start an elevated copy with the same options (UAC prompt)."""
    options = ["--ip", args.ip, "--console-ip", args.console_ip, "--prefix", str(args.prefix),
               "--log-dir", str(args.log_dir)] + (["--adapter", args.adapter] if args.adapter else [])
    if getattr(sys, "frozen", False):            # the exe opens the GUI by itself
        exe, params = sys.executable, options
    else:
        exe, params = sys.executable, ["-m", "switch2_offline", "gui", *options]
    cwd = str(Path(__file__).resolve().parents[1])  # so `-m switch2_offline` resolves
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe,
                                        " ".join(f'"{p}"' for p in params), cwd, 1)


class App:
    def __init__(self, root: tk.Tk, args):
        self.root, self.args = root, args
        self.ip = args.ip
        self.adapter = args.adapter or DEFAULT_ADAPTER  # read by worker threads
        self.events: queue.Queue = queue.Queue()        # worker threads -> Tk thread
        self.net: OfflineNet | None = None
        self.guard: windows.IsolationGuard | None = None
        self.busy = False
        self.state: windows.AdapterState | None = None
        self.state_error = ""
        self.console_ok: bool | None = None
        self.last_check = "never"
        self.closing = False
        self.admin = windows.is_admin()

        root.title(f"{TITLE} {__version__}" + ("" if self.admin else "  (not administrator)"))
        root.minsize(660, 600)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        threading.Thread(target=self._load_adapters, daemon=True).start()
        threading.Thread(target=self._poll, daemon=True).start()
        root.after(150, self._pump)

    # ------------------------------------------------------------ layout
    def _build(self) -> None:
        pad = {"padx": 10, "pady": 4}
        ttk.Style().configure("Big.TButton", font=("Segoe UI", 11, "bold"), padding=(14, 8))

        if not self.admin:
            bar = tk.Frame(self.root, bg="#fff3cd")
            bar.pack(fill="x")
            tk.Label(bar, bg="#fff3cd", text="Not running as administrator: Start and Restore "
                     "need it.").pack(side="left", padx=10, pady=6)
            ttk.Button(bar, text="Relaunch as administrator",
                       command=self._relaunch).pack(side="right", padx=10, pady=4)

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        self.btn_start = ttk.Button(top, text="▶  Start offline mode", style="Big.TButton",
                                    command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(top, text="■  Stop & restore Ethernet", style="Big.TButton",
                                   command=self.stop)
        self.btn_stop.pack(side="left", padx=8)
        self.dhcp_var = tk.BooleanVar(value=self.args.dhcp)
        ttk.Checkbutton(top, text="Also run DHCP\n(often blocked by other software)",
                        variable=self.dhcp_var).pack(side="left", padx=8)

        pick = ttk.Frame(self.root)
        pick.pack(fill="x", padx=10)
        ttk.Label(pick, text="Wired adapter:").pack(side="left")
        self.adapter_var = tk.StringVar(value=self.adapter)
        self.adapter_box = ttk.Combobox(pick, textvariable=self.adapter_var, state="readonly",
                                        width=30, values=[self.adapter])
        self.adapter_box.pack(side="left", padx=6)
        self.adapter_box.bind("<<ComboboxSelected>>", lambda _e: self._select(self.adapter_var.get()))
        ttk.Label(pick, text="(the port the console is plugged into)", foreground="#555").pack(side="left")

        box = ttk.LabelFrame(self.root, text="Status")
        box.pack(fill="x", **pad)
        self.rows = {}
        for r, key in enumerate(ROWS):
            ttk.Label(box, text=key, width=10).grid(row=r, column=0, sticky="w", padx=(8, 0), pady=2)
            dot = tk.Label(box, text="●", fg=GREY, font=("Segoe UI", 11))
            dot.grid(row=r, column=1, padx=4)
            value = ttk.Label(box, text="checking…")
            value.grid(row=r, column=2, sticky="w")
            self.rows[key] = (dot, value)

        cfg = ttk.LabelFrame(self.root, text="Switch 2: System Settings › Internet › Internet "
                                             "Settings › Wired Connection › Change Settings")
        cfg.pack(fill="x", **pad)
        for i, (key, value) in enumerate(console_settings(self.ip, self.args.console_ip, self.args.prefix)):
            ttk.Label(cfg, text=key + ":").grid(row=i // 2, column=(i % 2) * 2, sticky="w", padx=(8, 4))
            ttk.Label(cfg, text=value, font=("Consolas", 10, "bold")).grid(
                row=i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=(0, 24))
        ttk.Label(cfg, text=f"Programs on this PC reach the console at {self.args.console_ip}. "
                            "Set it back to Automatic to use other networks.",
                  foreground="#555").grid(row=3, column=0, columnspan=4, sticky="w", padx=8, pady=(4, 6))

        logs = ttk.LabelFrame(self.root, text="Log")
        logs.pack(fill="both", expand=True, **pad)
        self.log = scrolledtext.ScrolledText(logs, height=12, font=("Consolas", 9),
                                             state="disabled", wrap="none")
        self.log.pack(fill="both", expand=True, padx=4, pady=4)

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=10, pady=(0, 10))
        self.btn_server = ttk.Button(bottom, command=self.toggle_server)
        self.btn_server.pack(side="left")
        ttk.Button(bottom, text="Open logs folder", command=self.open_logs).pack(side="left", padx=8)
        ttk.Button(bottom, text="Clear log", command=self.clear_log).pack(side="left")

    # ----------------------------------------------------------- helpers
    def emit(self, line: str) -> None:
        """Log a line from any thread."""
        self.events.put(("log", line))

    def _on_guard_report(self, tag: str, message: str) -> None:
        self.net.log(tag, message)  # the guard only runs while self.net exists
        if message.startswith("NOT ISOLATED"):
            self.events.put(("alert", message))

    def _on_server_log(self, line: str) -> None:
        if "connection check 200" in line:
            self.last_check = line[:8]
        self.emit(line)

    def _append(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 3000:
            self.log.delete("1.0", "500.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def open_logs(self) -> None:
        self.args.log_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(self.args.log_dir)

    def _relaunch(self) -> None:
        self._stop_server()
        relaunch_as_admin(self.args)
        self.root.destroy()

    def _select(self, adapter: str) -> None:
        self.adapter = adapter
        self.state, self.state_error, self.console_ok = None, "", None
        for row in ("Cable", "PC IP", "Firewall"):
            self._set(row, GREY, "checking…")  # until the next poll for this adapter

    def offline(self) -> bool:
        return bool(self.state and self.state.mode(self.ip) == "offline")

    def server_running(self) -> bool:
        return bool(self.net and self.net.running)

    # ----------------------------------------------------------- actions
    def _in_background(self, work) -> None:
        if self.busy:
            return
        self.busy = True

        def run():
            try:
                work()
            except windows.SetupError as exc:
                for line in str(exc).splitlines():
                    self.emit("ERROR " + line)
            except Exception as exc:  # show it rather than fail silently
                self.emit(f"ERROR {exc!r}")
            finally:
                self.busy = False
        threading.Thread(target=run, daemon=True).start()

    def _start_server(self) -> None:
        if not self.server_running():
            self.net = OfflineNet(self.ip, self.args.prefix, self.args.console_ip,
                                  dhcp=self.dhcp_var.get(), log_dir=self.args.log_dir,
                                  on_log=self._on_server_log)
            self.net.start()
            self.guard = windows.IsolationGuard(self.adapter, self.ip, self.args.prefix,
                                                report=self._on_guard_report)
            self.guard.start()

    def _stop_server(self) -> None:
        if self.guard:
            self.guard.stop()
            self.guard = None
        if self.server_running():
            self.net.stop()

    def start(self) -> None:
        def work():
            if windows.adapter_state(self.adapter).mode(self.ip) != "offline":
                self.emit(f"SETUP Putting '{self.adapter}' in offline mode…")
                windows.setup(self.adapter, self.ip, self.args.prefix)
                self.emit(f"SETUP '{self.adapter}' is now {self.ip}/{self.args.prefix}: "
                          "no gateway, firewall rules added.")
            self._start_server()
        self._in_background(work)

    def stop(self, then_close: bool = False) -> None:
        def work():
            self._stop_server()
            self.emit(f"SETUP Restoring '{self.adapter}'…")
            windows.restore(self.adapter, self.ip)
            self.emit(f"SETUP '{self.adapter}' restored to automatic IP; firewall rules removed.")
            if then_close:
                self.events.put(("close", None))
        self._in_background(work)

    def toggle_server(self) -> None:
        self._in_background(self._stop_server if self.server_running() else self._start_server)

    def on_close(self) -> None:
        if self.busy:
            return
        if self.offline() or self.server_running():
            answer = messagebox.askyesnocancel(
                TITLE, "Restore normal Ethernet before closing?\n\n"
                       "Yes: stop the server and restore the adapter.\n"
                       "No: stop the server, keep the adapter in offline mode.")
            if answer is None:
                return
            if answer:
                self.stop(then_close=True)
                return
        self._stop_server()
        self.closing = True
        self.root.destroy()

    # ------------------------------------------------------ worker threads
    def _load_adapters(self) -> None:
        try:
            self.events.put(("adapters", windows.wired_adapters(self.ip)))
        except windows.SetupError as exc:
            self.emit(f"ERROR listing adapters: {exc}")

    def _poll(self) -> None:
        while not self.closing:
            adapter = self.adapter
            try:
                state, error = windows.adapter_state(adapter), ""
            except windows.SetupError as exc:
                state, error = None, str(exc)
            console = None
            if state and state.link_up and self.ip in state.ips:
                console = windows.ping(self.args.console_ip)
            if adapter == self.adapter:  # ignore results for a previous selection
                self.events.put(("state", (state, error, console)))
            time.sleep(3)

    # -------------------------------------------------------- Tk thread
    def _pump(self) -> None:
        while not self.events.empty():
            kind, data = self.events.get_nowait()
            if kind == "log":
                self._append(data)
            elif kind == "state":
                self.state, self.state_error, self.console_ok = data
            elif kind == "adapters":
                wired, holder = data
                if wired:
                    self.adapter_box.configure(values=wired)
                    if not self.args.adapter and not (self.busy or self.server_running()):
                        wanted = windows.choose_adapter(None, wired, holder)
                        if wanted != self.adapter:
                            self.adapter_var.set(wanted)
                            self._select(wanted)
            elif kind == "alert":
                messagebox.showwarning(TITLE, data.removeprefix("NOT ISOLATED: ") +
                                       "\n\nThe console may be able to reach the internet "
                                       "through this PC until this is fixed.")
            elif kind == "close":
                self.closing = True
                self.root.destroy()
                return
        self._render()
        self.root.after(250, self._pump)

    def _set(self, row: str, color: str, text: str) -> None:
        dot, value = self.rows[row]
        dot.configure(fg=color)
        value.configure(text=text)

    def _render(self) -> None:
        s = self.state
        if self.state_error:
            self._set("Cable", RED, "status query failed: " + self.state_error[:100])
        elif s and not s.found:
            self._set("Cable", RED, f"adapter '{self.adapter}' not found: pick another above")
        elif s:
            self._set("Cable", GREEN if s.link_up else AMBER,
                      f"connected ({s.speed})" if s.link_up else f"{s.status}: plug in the console's LAN cable")
            mode = s.mode(self.ip)
            others = ", ".join(i for i in s.ips if not i.startswith("169.254.")) or "no address yet"
            self._set("PC IP", *{"offline": (GREEN, f"{self.ip}  (offline mode, no gateway)"),
                                 "normal": (GREY, f"normal, automatic ({others})"),
                                 "partial": (AMBER, "partly configured: press Start or Stop")}[mode])
            n = s.firewall_rules
            self._set("Firewall", GREEN if n >= windows.FIREWALL_RULES else (GREY if not n else AMBER),
                      f"{n}/{windows.FIREWALL_RULES} rules")

        if self.server_running():
            st = self.net.state
            color = RED if "error" in st.values() else AMBER if "waiting for link" in st.values() else GREEN
            self._set("Server", color, f"DNS {st['dns']} · HTTP {st['http']} · DHCP {st['dhcp']}")
        else:
            self._set("Server", GREY, "stopped")

        guard = self.guard
        if not guard or not guard.last_check:
            self._set("Isolation", GREY, "checked at Start and every 10 s while the server runs")
        elif guard.problems:
            self._set("Isolation", RED, "NOT ISOLATED: " + guard.problems[0][:110])
        elif guard.error:
            self._set("Isolation", AMBER, "could not check: " + guard.error[:100])
        else:
            self._set("Isolation", GREEN, "ok: no routing, forwarding, sharing, NAT or gateway "
                                          f"(checked {guard.last_check})")

        console = self.args.console_ip
        self._set("Console", *{None: (GREY, f"{console}: checked once the link is up in offline mode"),
                               True: (GREEN, f"{console} answers ping"),
                               False: (AMBER, f"{console} no reply (is the console's manual IP set?)")}
                  [self.console_ok])

        if self.net:
            st = self.net.stats
            self._set("Activity", GREEN if st["checks"] else GREY,
                      f"DNS {st['dns']} · connection checks {st['checks']} · rejected {st['rejected']}"
                      f" · DHCP {st['dhcp']} · last check {self.last_check}")
        else:
            self._set("Activity", GREY, "-")

        running = self.server_running()
        self.btn_start.configure(state="disabled" if self.busy or (running and self.offline()) else "normal")
        self.btn_stop.configure(state="disabled" if self.busy else "normal")
        self.btn_server.configure(state="disabled" if self.busy else "normal",
                                  text="Stop server only (keep IP)" if running else "Start server only")
        locked = self.busy or running or self.offline()
        self.adapter_box.configure(state="disabled" if locked else "readonly")


def run(args) -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on high-DPI screens
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    try:
        root.iconbitmap(default=str(Path(__file__).with_name("app.ico")))
    except tk.TclError:
        pass
    App(root, args)
    if os.environ.get("SWITCH2_OFFLINE_SMOKE_TEST"):  # used by the tests: open, then close
        root.after(int(os.environ["SWITCH2_OFFLINE_SMOKE_TEST"]), root.destroy)
    root.mainloop()
