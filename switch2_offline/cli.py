# SPDX-License-Identifier: GPL-2.0-or-later
"""Command line interface. Every GUI action is available here:

    python -m switch2_offline start      set up, run, restore on Ctrl+C
    python -m switch2_offline gui        the same, with a window
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import (DEFAULT_CONSOLE_IP, DEFAULT_IP, DEFAULT_PREFIX, __version__, console_settings,
               default_log_dir, validate_addresses, windows)
from .services import OfflineNet


def build_parser() -> argparse.ArgumentParser:
    addresses = argparse.ArgumentParser(add_help=False)
    group = addresses.add_argument_group("addresses")
    group.add_argument("--ip", default=DEFAULT_IP,
                       help="this PC's address on the cable (default: %(default)s)")
    group.add_argument("--console-ip", default=DEFAULT_CONSOLE_IP,
                       help="the console's address (default: %(default)s)")
    group.add_argument("--prefix", type=int, default=DEFAULT_PREFIX,
                       help="subnet prefix length (default: %(default)s)")

    adapter = argparse.ArgumentParser(add_help=False)
    adapter.add_argument("-a", "--adapter",
                         help="wired adapter name (default: the one already in offline mode, "
                              "else 'Ethernet', else the only wired adapter)")

    server = argparse.ArgumentParser(add_help=False)
    server.add_argument("--dhcp", action="store_true",
                        help="also run a DHCP server; often blocked by other software, so a "
                             "manual IP on the console is more reliable")
    server.add_argument("--log-dir", type=Path, default=default_log_dir(),
                        help=r"where log files go (default: %%LOCALAPPDATA%%\switch2-ethernet-offline\logs)")

    parser = argparse.ArgumentParser(
        prog="switch2-offline",
        description="Wired, internet-free link between this PC and a Switch 2 that still "
                    "passes the console's connection test.",
        epilog="Run 'switch2-offline <command> -h' for the options of a command.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    def command(name: str, text: str, parents: list) -> argparse.ArgumentParser:
        return commands.add_parser(name, help=text, description=text, parents=parents)

    start = command("start", "set up the adapter, run the services, and restore the adapter "
                             "on Ctrl+C (admin)", [adapter, addresses, server])
    start.add_argument("--keep", action="store_true",
                       help="leave the adapter in offline mode when stopping")
    command("serve", "run the services only; the adapter must already be set up "
                     "(no admin needed)", [addresses, server])
    command("setup", "put the adapter in offline mode and leave it there (admin)",
            [adapter, addresses])
    command("restore", "return the adapter to automatic IP and remove the firewall "
                       "rules (admin)", [adapter, addresses])
    command("status", "show the adapter, firewall, isolation and console state",
            [adapter, addresses])
    command("adapters", "list wired adapters", [addresses])
    command("gui", "open the control window", [adapter, addresses, server])
    return parser


# ---------------------------------------------------------------- output
def print_rows(rows: list[tuple[str, str]]) -> None:
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"  {key:<{width}}  {value}")


def print_console_settings(args) -> None:
    print("On the Switch 2: System Settings > Internet > Internet Settings > "
          "Wired Connection > Change Settings")
    print_rows(console_settings(args.ip, args.console_ip, args.prefix))


# -------------------------------------------------------------- commands
def serve(args, adapter: str | None = None) -> None:
    """Run the services until Ctrl+C, re-checking isolation every 10 seconds."""
    net = OfflineNet(args.ip, args.prefix, args.console_ip, dhcp=args.dhcp, log_dir=args.log_dir)
    net.start()
    guard = windows.IsolationGuard(adapter, args.ip, args.prefix, report=net.log)
    guard.start()
    print("Running. Press Ctrl+C to stop.", flush=True)
    try:
        while True:
            time.sleep(60)
            net.log("STAT", net.stats_line() + (" NOT-ISOLATED" if guard.problems else ""))
    except KeyboardInterrupt:
        pass
    finally:
        guard.stop()
        net.stop()


def cmd_start(args) -> None:
    windows.require_admin()
    adapter = windows.resolve_adapter(args.adapter, args.ip)
    if windows.adapter_state(adapter).mode(args.ip) == "offline":
        print(f"'{adapter}' is already in offline mode.")
    else:
        windows.setup(adapter, args.ip, args.prefix)
        print(f"'{adapter}' is now {args.ip}/{args.prefix}: no gateway, firewall rules added.")
    print_console_settings(args)
    try:
        serve(args, adapter)
    finally:
        if args.keep:
            print(f"'{adapter}' stays in offline mode. Undo with: switch2-offline restore")
        else:
            windows.restore(adapter, args.ip)
            print(f"'{adapter}' restored to automatic IP.")


def cmd_serve(args) -> None:
    serve(args)


def cmd_setup(args) -> None:
    adapter = windows.resolve_adapter(args.adapter, args.ip)
    windows.setup(adapter, args.ip, args.prefix)
    print(f"'{adapter}' is now {args.ip}/{args.prefix}: no gateway, firewall rules added.")
    print_console_settings(args)
    print("Next: switch2-offline serve   (undo later with: switch2-offline restore)")


def cmd_restore(args) -> None:
    adapter = windows.resolve_adapter(args.adapter, args.ip)
    windows.restore(adapter, args.ip)
    print(f"'{adapter}' restored to automatic IP; firewall rules removed.")


def cmd_status(args) -> None:
    adapter = windows.resolve_adapter(args.adapter, args.ip)
    s = windows.adapter_state(adapter)
    if not s.found:
        raise windows.SetupError(f"adapter '{adapter}' not found (list them with 'adapters')")
    mode = {"offline": f"offline ({args.ip}/{args.prefix}, no gateway)",
            "normal": "normal (automatic IP)",
            "partial": "partly configured: run 'setup' or 'restore'"}[s.mode(args.ip)]
    rows = [("Adapter", adapter), ("Link", f"{s.status} {s.speed}".strip()), ("Mode", mode),
            ("Addresses", ", ".join(s.ips) or "-"),
            ("Firewall", f"{s.firewall_rules}/{windows.FIREWALL_RULES} rules")]
    if s.link_up and args.ip in s.ips:
        reply = "answers ping" if windows.ping(args.console_ip) else "no reply"
        rows.append(("Console", f"{args.console_ip} {reply}"))
    problems = windows.isolation_problems(adapter, args.ip, args.prefix,
                                          offline=s.mode(args.ip) == "offline")
    rows.append(("Isolation", "ok" if not problems else "NOT ISOLATED: " + " ".join(problems)))
    print_rows(rows)


def cmd_adapters(args) -> None:
    wired, holder = windows.wired_adapters(args.ip)
    if not wired:
        raise windows.SetupError("no wired Ethernet adapter found")
    for name in wired:
        print(name + (f"   (offline mode, {args.ip})" if name == holder else ""))


def cmd_gui(args) -> None:
    from .gui import run  # tkinter is only needed for this command
    run(args)


COMMANDS = {"start": cmd_start, "serve": cmd_serve, "setup": cmd_setup, "restore": cmd_restore,
            "status": cmd_status, "adapters": cmd_adapters, "gui": cmd_gui}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_addresses(args.ip, args.prefix, args.console_ip)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        COMMANDS[args.command](args)
    except windows.SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def gui_main() -> None:
    """Entry point of the windowed exe and `switch2-offline-gui`: options as for `gui`."""
    if sys.stdout is None:  # windowed builds have no console; keep argparse from crashing
        sys.stdout = sys.stderr = open(os.devnull, "w")
    sys.exit(main(["gui", *sys.argv[1:]]))
