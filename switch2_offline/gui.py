# SPDX-License-Identifier: GPL-2.0-or-later
"""Control window (tkinter). It runs exactly what the CLI runs:
windows.setup/restore for the adapter and services.OfflineNet for the link.

Worker threads never touch Tk: they put events on self.events, and _pump
handles them on the Tk thread.
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

from . import DEFAULT_ADAPTER, __version__, console_settings, default_log_dir, windows
from .services import OfflineNet
from .tray import TrayIcon, message, open_running_copy

TITLE = "Switch 2 Offline Ethernet"
ICON = Path(__file__).with_name("app.ico")
SETTINGS = default_log_dir().parent / "settings.json"
GREEN, AMBER, RED, GREY, MUTED = "#1a9c4a", "#c98a00", "#c0392b", "#9a9a9a", "#666666"

# Status rows and the explanation shown when hovering over their names.
ROWS = {
    "Cable": "The Ethernet link between this PC and the Switch 2's dock (its LAN port).",
    "This PC": "This PC's address on the cable. Offline mode uses a fixed address with no "
               "gateway, so nothing is routed to the internet.",
    "Firewall": "Inbound rules that let the Switch 2 reach this app (DNS, HTTP, DHCP, ping), "
                "on this adapter only.",
    "Services": "What answers the Switch 2. DNS sends every name to this PC, HTTP answers only "
                "Nintendo's connection test, DHCP is optional.",
    "Internet": "Re-checked every 10 seconds while running: no routing, forwarding, sharing, "
                "NAT or gateway that could let the Switch 2 reach the internet.",
    "Switch 2": "Whether the Switch 2 answers ping at its manual address. Programs on this PC "
                "reach it at that address.",
    "Activity": "Name lookups answered, connection tests passed (the Switch 2 then shows "
                "connected), requests refused, and DHCP replies.",
}


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    try:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(settings), encoding="utf-8")
    except OSError:
        pass


class Tooltip:
    """A short explanation shown while the mouse rests on a widget."""

    def __init__(self, widget, text: str):
        self.widget, self.text, self.tip, self.job = widget, text, None, None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._hide()
        self.job = self.widget.after(450, self._show)

    def _show(self) -> None:
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{self.widget.winfo_rootx() + 12}"
                             f"+{self.widget.winfo_rooty() + self.widget.winfo_height() + 4}")
        tk.Label(self.tip, text=self.text, justify="left", wraplength=380, bg="#ffffee",
                 relief="solid", borderwidth=1, padx=7, pady=4).pack()

    def _hide(self, _event=None) -> None:
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.tip:
            self.tip.destroy()
            self.tip = None


# ------------------------------------------------- one copy at a time
_instance_lock = None


def claim_single_instance() -> bool:
    """False if another copy of the window is already open."""
    global _instance_lock
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, "Local\\Switch2OfflineEthernetGui")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _instance_lock = handle
    return True


def release_single_instance() -> None:
    global _instance_lock
    if _instance_lock:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(_instance_lock))
        _instance_lock = None


def relaunch_as_admin(args, dhcp: bool) -> None:
    """Start an elevated copy with the same options (UAC prompt)."""
    program, arguments, folder = windows.gui_command(windows.gui_options(args, dhcp))
    release_single_instance()  # the new copy must be able to claim it
    ctypes.windll.shell32.ShellExecuteW(None, "runas", program,
                                        subprocess.list2cmdline(arguments), folder, 1)


class App:
    def __init__(self, root: tk.Tk, args):
        self.root, self.args = root, args
        self.ip = args.ip
        self.adapter = args.adapter or DEFAULT_ADAPTER  # read by worker threads
        self.events: queue.Queue = queue.Queue()        # worker threads -> Tk thread
        self.net: OfflineNet | None = None
        self.guard: windows.IsolationGuard | None = None
        self.busy = ""          # what a running action is doing, for the headline
        self.last_error = ""    # first line of the last failed action
        self.state: windows.AdapterState | None = None
        self.state_error = ""
        self.console_ok: bool | None = None
        self.last_check = "never"
        self.closing = False
        self.admin = windows.is_admin()
        self.settings = load_settings()
        self.tray = TrayIcon(TITLE, ICON, on_open=lambda: self.events.put(("show", None)),
                             on_exit=lambda: self.events.put(("exit", None)))

        root.title(f"{TITLE} {__version__}" + ("" if self.admin else "  (not administrator)"))
        root.minsize(640, 0)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Unmap>", self._on_unmap)
        self._build()
        threading.Thread(target=self._load_choices, daemon=True).start()
        threading.Thread(target=self._poll, daemon=True).start()
        if args.tray:
            self.hide_to_tray()
        root.after(150, self._pump)

    # ------------------------------------------------------------ layout
    def _build(self) -> None:
        style = ttk.Style()
        style.configure("Big.TButton", font=("Segoe UI", 11, "bold"), padding=(14, 8))
        style.configure("Headline.TLabel", font=("Segoe UI Semibold", 15))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Value.TLabel", font=("Consolas", 10, "bold"))
        outer = ttk.Frame(self.root, padding=(14, 10, 14, 12))
        outer.pack(fill="both", expand=True)

        if not self.admin:
            bar = tk.Frame(outer, bg="#fff3cd")
            bar.pack(fill="x", pady=(0, 8))
            tk.Label(bar, bg="#fff3cd", text="Not running as administrator: Start, Stop and "
                     "Start with Windows need it.").pack(side="left", padx=8, pady=6)
            ttk.Button(bar, text="Relaunch as administrator",
                       command=self._relaunch).pack(side="right", padx=8, pady=4)

        # What is happening, in one line, and the two main buttons.
        head = ttk.Frame(outer)
        head.pack(fill="x")
        self.head_dot = tk.Label(head, text="●", font=("Segoe UI", 20), fg=GREY)
        self.head_dot.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 8))
        self.headline = ttk.Label(head, text="Checking…", style="Headline.TLabel")
        self.headline.grid(row=0, column=1, sticky="w")
        self.subline = ttk.Label(head, style="Muted.TLabel", wraplength=560)
        self.subline.grid(row=1, column=1, sticky="w")

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(10, 4))
        self.btn_start = ttk.Button(buttons, text="▶  Start offline mode", style="Big.TButton",
                                    command=self.start)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(buttons, text="■  Stop and restore Ethernet",
                                   style="Big.TButton", command=self.stop)
        self.btn_stop.pack(side="left", padx=8)
        Tooltip(self.btn_start, "Puts this PC's Ethernet in offline mode (fixed address, no "
                                "gateway, firewall rules) and starts answering the Switch 2.")
        Tooltip(self.btn_stop, "Stops answering, returns Ethernet to automatic IP and removes "
                               "the firewall rules.")

        status = ttk.LabelFrame(outer, text="Status", padding=(8, 2, 8, 4))
        status.pack(fill="x", pady=(8, 0))
        self.rows = {}
        for r, (name, explanation) in enumerate(ROWS.items()):
            label = ttk.Label(status, text=name, style="Muted.TLabel", width=9, cursor="question_arrow")
            label.grid(row=r, column=0, sticky="w", pady=1)
            Tooltip(label, explanation)
            dot = tk.Label(status, text="●", fg=GREY, font=("Segoe UI", 10))
            dot.grid(row=r, column=1, padx=(0, 6))
            value = ttk.Label(status, text="checking…")
            value.grid(row=r, column=2, sticky="w")
            self.rows[name] = (dot, value)

        switch = ttk.LabelFrame(outer, text="On the Switch 2 (enter once)", padding=(8, 2, 8, 6))
        switch.pack(fill="x", pady=(8, 0))
        ttk.Label(switch, text="System Settings › Internet › Internet Settings › Wired "
                               "Connection › Change Settings", style="Muted.TLabel"
                  ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 4))
        for i, (key, value) in enumerate(console_settings(self.ip, self.args.console_ip, self.args.prefix)):
            ttk.Label(switch, text=key).grid(row=1 + i // 2, column=(i % 2) * 2, sticky="w", padx=(0, 8))
            ttk.Label(switch, text=value, style="Value.TLabel").grid(
                row=1 + i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=(0, 22))
        ttk.Label(switch, text="Set it back to Automatic afterwards to use other networks.",
                  style="Muted.TLabel").grid(row=4, column=0, columnspan=4, sticky="w", pady=(4, 0))

        options = ttk.Frame(outer)
        options.pack(fill="x", pady=(10, 0))
        ttk.Label(options, text="Adapter").pack(side="left")
        self.adapter_var = tk.StringVar(value=self.adapter)
        self.adapter_box = ttk.Combobox(options, textvariable=self.adapter_var, state="readonly",
                                        width=16, values=[self.adapter])
        self.adapter_box.pack(side="left", padx=(4, 14))
        self.adapter_box.bind("<<ComboboxSelected>>", lambda _e: self._select(self.adapter_var.get()))
        Tooltip(self.adapter_box, "The Ethernet port the Switch 2 is plugged into. Locked while in use.")
        self.dhcp_var = tk.BooleanVar(value=self.args.dhcp)
        self.tray_var = tk.BooleanVar(value=self.settings.get("tray", False))
        self.startup_var = tk.BooleanVar(value=False)
        boxes = []
        for text, var, command, explanation in (
                ("Run DHCP", self.dhcp_var, None,
                 "Also hand the Switch 2 its address automatically, from the next Start. Often "
                 "blocked by security software, so the manual settings above are more reliable."),
                ("Minimize to tray", self.tray_var, self._save_settings,
                 "Minimizing hides the window in the notification area, next to the clock. "
                 "Click the icon to bring it back."),
                ("Start with Windows", self.startup_var, self.toggle_startup,
                 "Opens this window in the tray when you sign in. It does not change Ethernet "
                 "until you press Start.")):
            box = ttk.Checkbutton(options, text=text, variable=var, command=command)
            box.pack(side="left", padx=(0, 12))
            Tooltip(box, explanation)
            boxes.append(box)
        self.startup_box = boxes[-1]

        details = ttk.Frame(outer)
        details.pack(fill="x", pady=(8, 0))
        self.btn_log = ttk.Button(details, style="Toolbutton",
                                  command=lambda: self.toggle_log(remember=True))
        self.btn_log.pack(side="left")
        self.btn_clear = ttk.Button(details, text="Clear", command=self.clear_log)
        self.btn_logs = ttk.Button(details, text="Logs folder", command=self.open_logs)
        self.btn_logs.pack(side="right", padx=(4, 0))
        self.btn_server = ttk.Button(details, command=self.toggle_server)
        self.btn_server.pack(side="right")
        Tooltip(self.btn_server, "Runs or stops the services without changing the adapter "
                                 "(like the serve command).")
        self.log_frame = ttk.Frame(outer)
        self.log = scrolledtext.ScrolledText(self.log_frame, height=12, font=("Consolas", 9),
                                             state="disabled", wrap="none")
        self.log.pack(fill="both", expand=True)
        self.toggle_log(self.settings.get("show_log", False))

    # ----------------------------------------------------------- helpers
    def emit(self, line: str) -> None:
        """Log a line from any thread."""
        self.events.put(("log", line))

    def _on_server_log(self, line: str) -> None:
        if "connection check 200" in line:
            self.last_check = line[:8]
        self.emit(line)

    def _on_guard_report(self, tag: str, text: str) -> None:
        self.net.log(tag, text)  # the guard only runs while self.net exists
        if text.startswith("NOT ISOLATED"):
            self.events.put(("alert", text))

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

    def toggle_log(self, show: bool | None = None, remember: bool = False) -> None:
        show = not self.log_frame.winfo_ismapped() if show is None else show
        if show:
            self.log_frame.pack(fill="both", expand=True, pady=(4, 0))
            self.btn_clear.pack(side="right", padx=(4, 0), before=self.btn_logs)
        else:
            self.log_frame.pack_forget()
            self.btn_clear.pack_forget()
        self.btn_log.configure(text="▾  Hide log" if show else "▸  Show log")
        if self.root.state() == "normal":
            self.root.geometry("")  # grow or shrink to fit
        if remember:
            self.settings["show_log"] = show
            save_settings(self.settings)

    def _save_settings(self) -> None:
        self.settings["tray"] = self.tray_var.get()
        save_settings(self.settings)

    def open_logs(self) -> None:
        self.args.log_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(self.args.log_dir)

    def _relaunch(self) -> None:
        self._stop_server()
        relaunch_as_admin(self.args, self.dhcp_var.get())
        self.quit()

    def _select(self, adapter: str) -> None:
        self.adapter = adapter
        self.state, self.state_error, self.console_ok = None, "", None
        for row in ("Cable", "This PC", "Firewall"):
            self._set(row, GREY, "checking…")  # until the next poll for this adapter

    def offline(self) -> bool:
        return bool(self.state and self.state.mode(self.ip) == "offline")

    def server_running(self) -> bool:
        return bool(self.net and self.net.running)

    # -------------------------------------------------------- the tray
    def _on_unmap(self, event) -> None:
        if event.widget is self.root and self.tray_var.get() and self.root.state() == "iconic":
            self.hide_to_tray()

    def hide_to_tray(self) -> None:
        if self.tray.show():
            self.root.withdraw()
        else:
            self.root.iconify()  # no tray available: an ordinary minimize

    def show_window(self) -> None:
        self.tray.hide()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit(self) -> None:
        self.closing = True
        self.tray.hide()
        self.root.destroy()

    # ----------------------------------------------------------- actions
    def _in_background(self, work, doing: str) -> None:
        if self.busy:
            return
        self.busy, self.last_error = doing, ""

        def run():
            try:
                work()
            except windows.SetupError as exc:
                self.last_error = str(exc).splitlines()[0]
                for line in str(exc).splitlines():
                    self.emit("ERROR " + line)
            except Exception as exc:  # show it rather than fail silently
                self.last_error = repr(exc)
                self.emit(f"ERROR {exc!r}")
            finally:
                self.busy = ""
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
        self._in_background(work, "Setting up Ethernet and starting the services…")

    def stop(self, then_close: bool = False) -> None:
        def work():
            self._stop_server()
            self.emit(f"SETUP Restoring '{self.adapter}'…")
            windows.restore(self.adapter, self.ip)
            self.emit(f"SETUP '{self.adapter}' restored to automatic IP; firewall rules removed.")
            if then_close:
                self.events.put(("close", None))
        self._in_background(work, "Stopping and restoring normal Ethernet…")

    def toggle_server(self) -> None:
        if self.server_running():
            self._in_background(self._stop_server, "Stopping the services…")
        else:
            self._in_background(self._start_server, "Starting the services…")

    def toggle_startup(self) -> None:
        want, options = self.startup_var.get(), windows.gui_options(self.args, self.dhcp_var.get())

        def work():
            try:
                if want:
                    command = windows.enable_startup(options)
                    self.emit(f"SETUP Starts with Windows, in the tray: {command}")
                else:
                    windows.disable_startup()
                    self.emit("SETUP No longer starts with Windows.")
            except windows.SetupError as exc:
                self.emit(f"ERROR Start with Windows: {exc}")
                self.events.put(("startup", not want))  # untick: it did not change
        threading.Thread(target=work, daemon=True).start()

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
        self.quit()

    # ------------------------------------------------------ worker threads
    def _load_choices(self) -> None:
        try:
            self.events.put(("adapters", windows.wired_adapters(self.ip)))
            self.events.put(("startup", windows.startup_enabled()))
        except windows.SetupError as exc:
            self.emit(f"ERROR {exc}")

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
                if data.startswith("ERROR"):
                    self.toggle_log(True)
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
            elif kind == "startup":
                self.startup_var.set(data)
            elif kind == "show":
                self.show_window()
            elif kind == "exit":
                self.show_window()
                self.on_close()
            elif kind == "alert":
                self.show_window()
                messagebox.showwarning(TITLE, data.removeprefix("NOT ISOLATED: ") +
                                       "\n\nThe Switch 2 may be able to reach the internet "
                                       "through this PC until this is fixed.")
            elif kind == "close":
                self.quit()
                return
        self._render()
        self.root.after(250, self._pump)

    def _set(self, row: str, color: str, text: str) -> None:
        dot, value = self.rows[row]
        dot.configure(fg=color)
        value.configure(text=text)

    def _headline(self) -> tuple[str, str, str]:
        """(colour, headline, one-line explanation) for the current state."""
        s, running = self.state, self.server_running()
        if self.busy:
            return AMBER, "Working…", self.busy
        if self.last_error:
            return RED, "That didn't work", self.last_error + "  (details in the log)"
        if self.guard and self.guard.problems:
            return RED, "Internet is NOT blocked", self.guard.problems[0]
        if self.state_error:
            return RED, "Can't read the adapter", self.state_error[:150]
        if not s:
            return GREY, "Checking…", ""
        if not s.found:
            return RED, "Adapter not found", "Pick the port the Switch 2 is plugged into, under Adapter."
        mode = s.mode(self.ip)
        if mode == "partial":
            return AMBER, "Half configured", "Press Start to finish, or Stop to restore normal Ethernet."
        if mode == "offline" and running:
            if not s.link_up:
                return AMBER, "Waiting for the Switch 2", "Connect its dock's LAN port to this PC's Ethernet port."
            if self.console_ok is False:
                return GREEN, "Offline mode is on", "Enter the settings below on the Switch 2, then run Test Connection."
            return GREEN, "Offline mode is on", "The Switch 2 can connect: it shows online, but has no internet."
        if mode == "offline":
            return AMBER, "Not answering yet", "Ethernet is set up, but the services are stopped. Press Start."
        if running:
            return AMBER, "Ethernet not set up", "The services run, but the adapter is normal. Press Start."
        return GREY, "Off", "Ethernet works normally. Press Start to connect the Switch 2 offline."

    def _render(self) -> None:
        color, headline, subline = self._headline()
        self.head_dot.configure(fg=color)
        self.headline.configure(text=headline)
        self.subline.configure(text=subline)
        if self.tray.tooltip != f"{TITLE}\n{headline}":
            self.tray.set_tooltip(f"{TITLE}\n{headline}")

        s = self.state
        if s and s.found:
            self._set("Cable", GREEN if s.link_up else AMBER,
                      f"Connected · {s.speed}" if s.link_up else
                      ("Not connected" if s.status == "Disconnected" else s.status or "unknown"))
            others = ", ".join(i for i in s.ips if not i.startswith("169.254.")) or "no address yet"
            self._set("This PC", *{"offline": (GREEN, f"{self.ip} · offline mode, no gateway"),
                                   "normal": (GREY, f"Normal Ethernet · {others}"),
                                   "partial": (AMBER, "Half configured: press Start or Stop")}[s.mode(self.ip)])
            n, total = s.firewall_rules, windows.FIREWALL_RULES
            self._set("Firewall", GREEN if n >= total else (GREY if not n else AMBER),
                      f"{n} of {total} rules" if n else "No rules (Start adds them)")
        elif s or self.state_error:
            for row in ("Cable", "This PC", "Firewall"):
                self._set(row, RED, "-")

        if self.server_running():
            st = self.net.state
            color = RED if "error" in st.values() else AMBER if "waiting for link" in st.values() else GREEN
            self._set("Services", color, f"DNS {st['dns']} · HTTP {st['http']} · DHCP {st['dhcp']}")
        else:
            self._set("Services", GREY, "Stopped")

        guard = self.guard
        if not guard or not guard.last_check:
            self._set("Internet", GREY, "Checked while running")
        elif guard.problems:
            self._set("Internet", RED, "NOT BLOCKED: " + guard.problems[0][:110])
        elif guard.error:
            self._set("Internet", AMBER, "Could not check: " + guard.error[:100])
        else:
            self._set("Internet", GREEN, f"Blocked · checked {guard.last_check}")

        console = self.args.console_ip
        self._set("Switch 2", *{None: (GREY, f"{console} · checked once the cable is up in offline mode"),
                                True: (GREEN, f"Answers at {console}"),
                                False: (AMBER, f"No answer at {console}: is its manual IP set?")}
                  [self.console_ok])

        if self.net:
            st = self.net.stats
            self._set("Activity", GREEN if st["checks"] else GREY,
                      f"{st['dns']} lookups · {st['checks']} connection tests (last {self.last_check})"
                      f" · {st['rejected']} refused · {st['dhcp']} DHCP")
        else:
            self._set("Activity", GREY, "Nothing yet")

        running = self.server_running()
        normal = bool(s and s.found and s.mode(self.ip) == "normal")
        self.btn_start.configure(state="disabled" if self.busy or (running and self.offline()) else "normal")
        self.btn_stop.configure(state="disabled" if self.busy or (normal and not running) else "normal")
        self.btn_server.configure(state="disabled" if self.busy else "normal",
                                  text="Stop services only" if running else "Run services only")
        locked = self.busy or running or self.offline()
        self.adapter_box.configure(state="disabled" if locked else "readonly")
        self.startup_box.configure(state="normal" if self.admin else "disabled")


def run(args) -> None:
    if not claim_single_instance():
        if not open_running_copy():
            message(TITLE, f"{TITLE} is already open.")
        return
    try:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # sharp text on high-DPI screens
        except (AttributeError, OSError):
            pass
        root = tk.Tk()
        try:
            root.iconbitmap(default=str(ICON))
        except tk.TclError:
            pass
        App(root, args)
        if os.environ.get("SWITCH2_OFFLINE_SMOKE_TEST"):  # used by the tests: open, then close
            root.after(int(os.environ["SWITCH2_OFFLINE_SMOKE_TEST"]), root.destroy)
        root.mainloop()
    finally:
        release_single_instance()
