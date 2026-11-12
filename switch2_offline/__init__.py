# SPDX-License-Identifier: GPL-2.0-or-later
"""Switch 2 Offline Ethernet: a wired, internet-free link between a Windows PC
and a Nintendo Switch 2 that still passes the console's connection test."""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path

__version__ = "1.0.0"

DEFAULT_ADAPTER = "Ethernet"
DEFAULT_IP = "192.168.77.1"            # this PC, on the cable
DEFAULT_CONSOLE_IP = "192.168.77.100"  # the Switch 2
DEFAULT_PREFIX = 24
FIREWALL_GROUP = "Switch2 Offline Ethernet"


def validate_addresses(ip: str, prefix: int, console_ip: str) -> ipaddress.IPv4Network:
    """Return the link's network, or raise ValueError with a readable reason."""
    network = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
    if not ipaddress.IPv4Address(ip).is_private:
        raise ValueError(f"{ip} is not a private address")
    if console_ip == ip or ipaddress.IPv4Address(console_ip) not in network:
        raise ValueError(f"console address {console_ip} must be another address in {network}")
    return network


def console_settings(ip: str = DEFAULT_IP, console_ip: str = DEFAULT_CONSOLE_IP,
                     prefix: int = DEFAULT_PREFIX) -> list[tuple[str, str]]:
    """What to enter on the Switch 2 under Wired Connection > Change Settings."""
    mask = ipaddress.IPv4Network(f"0.0.0.0/{prefix}").netmask
    return [("IP Address Settings", "Manual"), ("IP Address", console_ip),
            ("Subnet Mask", str(mask)), ("Gateway", ip),
            ("DNS Settings", f"Manual: primary {ip}, secondary {ip}"), ("Proxy", "Off")]


def default_log_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "switch2-ethernet-offline" / "logs"
