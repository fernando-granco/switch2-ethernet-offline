# SPDX-License-Identifier: GPL-2.0-or-later
"""Put a Windows wired adapter in offline mode, and undo it.

Offline mode = static address, no gateway, no DNS servers, plus four inbound
firewall rules (DNS, HTTP, DHCP, ping) limited to that adapter. PowerShell
gathers facts and applies changes; the decisions (which adapter, whether the
link would really be isolated) are plain Python so they are easy to test.
"""
from __future__ import annotations

import ctypes
import ipaddress
import json
import subprocess
from dataclasses import dataclass, field

from . import DEFAULT_ADAPTER, FIREWALL_GROUP

FIREWALL_RULES = 4
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: no console flashes from the GUI


class SetupError(RuntimeError):
    """A change was refused or failed. The message is written for the user."""


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def require_admin() -> None:
    if not is_admin():
        raise SetupError("administrator rights are needed: run this from an elevated terminal")


def _quote(text) -> str:
    """PowerShell single-quoted string literal."""
    return "'" + str(text).replace("'", "''") + "'"


def _as_list(value) -> list:
    """ConvertTo-Json turns one-item arrays into scalars and empty ones into null."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _powershell(script: str, timeout: float = 90) -> str:
    wrapped = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8\n"
               "$ErrorActionPreference = 'Stop'\n"
               "try {\n" + script + "\n} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }")
    try:
        res = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", wrapped],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=NO_WINDOW)
    except FileNotFoundError:
        raise SetupError("PowerShell was not found; this tool needs Windows") from None
    except subprocess.TimeoutExpired:
        raise SetupError("PowerShell did not answer in time") from None
    if res.returncode:
        raise SetupError(res.stderr.strip() or f"PowerShell failed (exit {res.returncode})")
    return res.stdout


# ------------------------------------------------------------------ facts
@dataclass
class AdapterState:
    name: str
    found: bool = False
    wired: bool = False
    status: str = ""          # "Up", "Disconnected", ...
    speed: str = ""
    dhcp: bool = True
    ips: list = field(default_factory=list)
    firewall_rules: int = 0

    @property
    def link_up(self) -> bool:
        return self.status == "Up"

    def mode(self, ip: str) -> str:
        """'offline', 'normal' or 'partial' (a setup or restore did not finish)."""
        if ip in self.ips and not self.dhcp and self.firewall_rules >= FIREWALL_RULES:
            return "offline"
        if self.dhcp and ip not in self.ips and not self.firewall_rules:
            return "normal"
        return "partial"


def adapter_state(name: str) -> AdapterState:
    a = _quote(name)
    data = json.loads(_powershell(f"""
$a = Get-NetAdapter -Name {a} -ErrorAction SilentlyContinue
$i = Get-NetIPInterface -InterfaceAlias {a} -AddressFamily IPv4 -ErrorAction SilentlyContinue
[pscustomobject]@{{
  found = [bool]$a
  wired = [bool]($a -and $a.PhysicalMediaType -eq '802.3' -and -not $a.Virtual)
  status = "$($a.Status)"
  speed = "$($a.LinkSpeed)"
  dhcp = "$($i.Dhcp)" -ne 'Disabled'
  ips = @(Get-NetIPAddress -InterfaceAlias {a} -AddressFamily IPv4 -ErrorAction SilentlyContinue |
          ForEach-Object IPAddress)
  rules = @(Get-NetFirewallRule -Group {_quote(FIREWALL_GROUP)} -ErrorAction SilentlyContinue).Count
}} | ConvertTo-Json -Compress"""))
    return AdapterState(name, data["found"], data["wired"], data["status"], data["speed"],
                        data["dhcp"], _as_list(data["ips"]), data["rules"])


def wired_adapters(ip: str) -> tuple[list[str], str | None]:
    """Physical wired adapters, and the adapter (if any) that already has `ip`."""
    data = json.loads(_powershell(f"""
[pscustomobject]@{{
  wired = @(Get-NetAdapter -Physical | Where-Object PhysicalMediaType -eq '802.3' | ForEach-Object Name)
  holder = (Get-NetIPAddress -IPAddress {_quote(ip)} -ErrorAction SilentlyContinue |
            Select-Object -First 1).InterfaceAlias
}} | ConvertTo-Json -Compress"""))
    return _as_list(data["wired"]), data["holder"]


def choose_adapter(requested: str | None, wired: list[str], holder: str | None) -> str:
    """Pick the adapter: the one asked for, else the one already in offline
    mode, else 'Ethernet', else the only wired adapter."""
    if requested:
        return requested
    if holder:
        return holder
    if DEFAULT_ADAPTER in wired:
        return DEFAULT_ADAPTER
    if len(wired) == 1:
        return wired[0]
    if not wired:
        raise SetupError("no wired Ethernet adapter found")
    raise SetupError(f"several wired adapters ({', '.join(wired)}): choose one with --adapter")


def resolve_adapter(requested: str | None, ip: str) -> str:
    return requested or choose_adapter(None, *wired_adapters(ip))


def isolation_problems(adapter: str, ip: str, prefix: int) -> list[str]:
    facts = json.loads(_powershell("""
$ics = @()
try {
  $share = New-Object -ComObject HNetCfg.HNetShare
  foreach ($c in $share.EnumEveryConnection) {
    if ($share.INetSharingConfigurationForINetConnection($c).SharingEnabled) {
      $ics += $share.NetConnectionProps($c).Name
    }
  }
} catch { $ics = $null }
[pscustomobject]@{
  router = [int](Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters').IPEnableRouter
  forwarding = @(Get-NetIPInterface -AddressFamily IPv4 | Where-Object Forwarding -eq 'Enabled' |
                 ForEach-Object InterfaceAlias)
  ics = $ics
  addresses = @(Get-NetIPAddress -AddressFamily IPv4 |
                ForEach-Object { [pscustomobject]@{ alias = $_.InterfaceAlias; ip = $_.IPAddress } })
} | ConvertTo-Json -Compress -Depth 3"""))
    return find_isolation_problems(facts, adapter, ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False))


def find_isolation_problems(facts: dict, adapter: str, network: ipaddress.IPv4Network) -> list[str]:
    """Reasons the console could reach the internet, or the subnet would clash."""
    problems = []
    if facts.get("router"):
        problems.append("Windows IP routing is on (IPEnableRouter). Turn it off first.")
    if forwarding := _as_list(facts.get("forwarding")):
        problems.append(f"IPv4 forwarding is on for: {', '.join(forwarding)}. Turn it off first.")
    if facts.get("ics") is None:
        problems.append("could not check Internet Connection Sharing; make sure it is off.")
    elif shared := _as_list(facts["ics"]):
        problems.append(f"Internet Connection Sharing is on for: {', '.join(shared)}. Turn it off first.")
    clash = sorted({a["alias"] for a in _as_list(facts.get("addresses"))
                    if a["alias"] != adapter and ipaddress.IPv4Address(a["ip"]) in network})
    if clash:
        problems.append(f"{network} is already used by: {', '.join(clash)}. "
                        "Disconnect that network or choose another --ip.")
    return problems


def ping(ip: str) -> bool:
    try:
        res = subprocess.run(["ping", "-n", "1", "-w", "600", ip], capture_output=True, text=True,
                             errors="replace", timeout=5, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "TTL=" in res.stdout


# ---------------------------------------------------------------- changes
def setup(adapter: str, ip: str, prefix: int) -> None:
    """Static ip/prefix with no gateway or DNS, plus the firewall rules.
    Refuses if the adapter is not wired or the link would not be isolated."""
    require_admin()
    state = adapter_state(adapter)
    if not state.found:
        raise SetupError(f"adapter '{adapter}' not found (list them with the 'adapters' command)")
    if not state.wired:
        raise SetupError(f"'{adapter}' is not a physical wired adapter")
    if problems := isolation_problems(adapter, ip, prefix):
        raise SetupError("refusing to set up:\n  - " + "\n  - ".join(problems))
    a, group = _quote(adapter), _quote(FIREWALL_GROUP)
    _powershell(f"""
$a = {a}; $ip = {_quote(ip)}; $group = {group}
Set-NetIPInterface -InterfaceAlias $a -AddressFamily IPv4 -Dhcp Disabled
Get-NetRoute -InterfaceAlias $a -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
  Remove-NetRoute -Confirm:$false
Get-NetIPAddress -InterfaceAlias $a -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Remove-NetIPAddress -Confirm:$false
New-NetIPAddress -InterfaceAlias $a -IPAddress $ip -PrefixLength {int(prefix)} | Out-Null
Set-DnsClientServerAddress -InterfaceAlias $a -ResetServerAddresses
Set-DnsClient -InterfaceAlias $a -RegisterThisConnectionsAddress $false
Get-NetFirewallRule -Group $group -ErrorAction SilentlyContinue | Remove-NetFirewallRule
$rule = @{{ Group = $group; Direction = 'Inbound'; Action = 'Allow'; Profile = 'Any'; InterfaceAlias = $a }}
New-NetFirewallRule @rule -DisplayName "$group DNS"  -Protocol UDP -LocalPort 53 -LocalAddress $ip | Out-Null
New-NetFirewallRule @rule -DisplayName "$group HTTP" -Protocol TCP -LocalPort 80 -LocalAddress $ip | Out-Null
New-NetFirewallRule @rule -DisplayName "$group ping" -Protocol ICMPv4 -IcmpType 8 -LocalAddress $ip | Out-Null
# DHCP requests come from 0.0.0.0 to 255.255.255.255, so no address filter here.
New-NetFirewallRule @rule -DisplayName "$group DHCP" -Protocol UDP -LocalPort 67 -RemotePort 68 | Out-Null
""")


def restore(adapter: str, ip: str) -> None:
    """Undo setup(): automatic IP and DNS again, firewall rules removed."""
    require_admin()
    if not adapter_state(adapter).found:
        raise SetupError(f"adapter '{adapter}' not found (list them with the 'adapters' command)")
    _powershell(f"""
$a = {_quote(adapter)}
Get-NetFirewallRule -Group {_quote(FIREWALL_GROUP)} -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Get-NetIPAddress -InterfaceAlias $a -AddressFamily IPv4 -IPAddress {_quote(ip)} -ErrorAction SilentlyContinue |
  Remove-NetIPAddress -Confirm:$false
Set-NetIPInterface -InterfaceAlias $a -AddressFamily IPv4 -Dhcp Enabled
Set-DnsClientServerAddress -InterfaceAlias $a -ResetServerAddresses
Set-DnsClient -InterfaceAlias $a -RegisterThisConnectionsAddress $true
""")
