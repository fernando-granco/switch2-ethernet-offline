# SPDX-License-Identifier: GPL-2.0-or-later
"""DNS, HTTP and optional DHCP for the isolated link.

* DNS  udp/53: every A query is answered with this PC's address. Nothing is
  forwarded, so no name ever resolves to the internet.
* HTTP tcp/80: Nintendo's connection-test hosts get the reply the console
  expects (200 + "X-Organization: Nintendo"); every other host gets 404.
* DHCP udp/67 (optional): hands out the console address, with this PC as
  gateway and DNS. Windows never routes, so the gateway leads nowhere.

The connection-test hosts and responses are adapted from 90DNS by Ave,
https://gitlab.com/a/90dns (revision 4d477d71). Standard library only.
"""
from __future__ import annotations

import datetime as dt
import errno
import http.server
import ipaddress
import socket
import struct
import threading
from pathlib import Path
from typing import Callable

from . import DEFAULT_CONSOLE_IP, DEFAULT_IP, DEFAULT_PREFIX, validate_addresses

LEGACY_CHECK_HOST = "conntest.nintendowifi.net"
LEGACY_CHECK_BODY = (
    '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
    '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">'
    '<html><head><title>HTML Page</title></head><body bgcolor="#FFFFFF">'
    "This is test.html page</body></html>"
).encode()
CHECK_HOSTS = frozenset({
    "ctest.cdn.nintendo.net",
    "ctest.p01.ctest.srv.nintendo.net",  # used by the Switch 2 connection test
    "ctest-dl-lp1.cdn.nintendo.net",
    "ctest-ul-lp1.cdn.nintendo.net",
    "api.p01.lp1.ctest.srv.nintendo.net",
    "api-ipv6.p01.lp1.ctest.srv.nintendo.net",
})
QTYPE_NAMES = {1: "A", 28: "AAAA", 33: "SRV", 65: "HTTPS", 255: "ANY"}


# ---------------------------------------------------------------------- DNS
def build_dns_reply(query: bytes, answer_ip: str, ttl: int = 30):
    """Answer one DNS query. Returns (reply, name, qtype), or None to ignore it.

    A/ANY get `answer_ip`; every other type gets an empty NOERROR answer."""
    if len(query) < 12:
        return None
    tid, flags, qdcount = struct.unpack("!HHH", query[:6])
    if flags & 0x8000 or qdcount != 1:          # a response, or not one question
        return None
    pos, labels = 12, []
    while True:                                 # read the question name
        if pos >= len(query):
            return None
        length = query[pos]
        if length == 0:
            pos += 1
            break
        if length & 0xC0 or pos + 1 + length > len(query):
            return None
        labels.append(query[pos + 1:pos + 1 + length])
        pos += 1 + length
    if pos + 4 > len(query):
        return None
    qtype, qclass = struct.unpack("!HH", query[pos:pos + 4])
    name = b".".join(labels).decode("ascii", "replace").lower()
    opcode = (flags >> 11) & 0xF
    # Response + authoritative + recursion available; echo RD; NOTIMP for odd opcodes.
    rflags = 0x8000 | 0x0400 | 0x0080 | (flags & 0x0100) | (4 if opcode else 0)
    answer = b""
    if not opcode and qtype in (1, 255) and qclass in (1, 255):
        answer = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, ttl, 4) + socket.inet_aton(answer_ip)
    header = struct.pack("!HHHHHH", tid, rflags, 1, 1 if answer else 0, 0, 0)
    return header + query[12:pos + 4] + answer, name, qtype


# --------------------------------------------------------------------- HTTP
class CheckHandler(http.server.BaseHTTPRequestHandler):
    """Answers the connection test; 404 for anything else."""
    protocol_version = "HTTP/1.1"
    server_version, sys_version = "nginx", ""

    def _reply(self, send_body: bool) -> None:
        host = (self.headers.get("Host") or "").strip().lower().split(":")[0].rstrip(".")
        if host == LEGACY_CHECK_HOST:
            status, ctype, body = 200, "text/html", LEGACY_CHECK_BODY
        elif host in CHECK_HOSTS:
            status, ctype, body = 200, "text/plain", b"ok"
        else:
            status, ctype, body = 404, "text/plain", b"Offline link: no internet.\n"
        net: OfflineNet = self.server.owner
        net.count("checks" if status == 200 else "rejected")
        net.log("HTTP", f"{self.client_address[0]} {self.command} {host[:100]} {self.path[:100]} "
                        f"-> {'connection check 200' if status == 200 else '404'}")
        self.send_response(status)
        if status == 200:
            self.send_header("X-Organization", "Nintendo")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if send_body:
            self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        self._reply(True)

    do_POST = do_GET

    def do_HEAD(self):
        self._reply(False)

    def log_message(self, fmt, *args):  # OfflineNet.log replaces this
        pass


class _HTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, sock: socket.socket, owner: "OfflineNet"):
        super().__init__(sock.getsockname(), CheckHandler, bind_and_activate=False)
        self.socket.close()
        self.socket, self.owner = sock, owner  # use our already-bound socket
        self.server_activate()


# --------------------------------------------------------------------- DHCP
DHCP_MAGIC = b"\x63\x82\x53\x63"
DISCOVER, OFFER, REQUEST, DECLINE, ACK, NAK, RELEASE, INFORM = range(1, 9)
DHCP_NAMES = dict(zip(range(1, 9), "DISCOVER OFFER REQUEST DECLINE ACK NAK RELEASE INFORM".split()))


def parse_dhcp_options(data: bytes) -> dict[int, bytes]:
    opts, i = {}, 0
    while i + 1 < len(data) and data[i] != 255:
        if data[i] == 0:                        # pad
            i += 1
            continue
        code, length = data[i], data[i + 1]
        opts[code] = data[i + 2:i + 2 + length]
        i += 2 + length
    return opts


class DhcpServer:
    """Minimal DHCP server: the first client gets `first_ip`, the next the following one."""

    def __init__(self, server_ip: str, network: ipaddress.IPv4Network, first_ip: str,
                 lease_seconds: int = 86400):
        self.server_ip = ipaddress.IPv4Address(server_ip)
        self.network, self.lease = network, lease_seconds
        self.first = ipaddress.IPv4Address(first_ip)
        self.leases: dict[bytes, ipaddress.IPv4Address] = {}

    def allocate(self, mac: bytes) -> ipaddress.IPv4Address | None:
        if mac not in self.leases:
            used = set(self.leases.values()) | {self.server_ip}
            free = (ip for ip in self.network.hosts() if ip >= self.first and ip not in used)
            ip = next(free, None)
            if ip is None:
                return None
            self.leases[mac] = ip
        return self.leases[mac]

    def _reply(self, req: bytes, msg_type: int, yiaddr: ipaddress.IPv4Address | None) -> bytes:
        hdr = bytearray(236)
        hdr[0:3] = bytes([2, req[1], req[2]])   # BOOTREPLY, hardware type and length
        hdr[4:8] = req[4:8]                     # transaction id
        hdr[10:12] = req[10:12]                 # flags
        if yiaddr is None and msg_type == ACK:  # INFORM: echo the client's address
            hdr[12:16] = req[12:16]
        if yiaddr is not None:
            hdr[16:20] = yiaddr.packed
        hdr[28:44] = req[28:44]                 # client hardware address
        me = self.server_ip.packed
        opts = bytes([53, 1, msg_type, 54, 4]) + me
        if msg_type != NAK:
            opts += bytes([1, 4]) + self.network.netmask.packed
            opts += bytes([3, 4]) + me + bytes([6, 4]) + me  # gateway and DNS: this PC
            opts += bytes([28, 4]) + self.network.broadcast_address.packed
            if yiaddr is not None:
                opts += bytes([51, 4]) + struct.pack("!I", self.lease)
                opts += bytes([58, 4]) + struct.pack("!I", self.lease // 2)
                opts += bytes([59, 4]) + struct.pack("!I", self.lease * 7 // 8)
        return bytes(hdr) + DHCP_MAGIC + opts + b"\xff"

    def handle(self, pkt: bytes) -> tuple[bytes | None, str | None]:
        """Return (reply or None, log text or None)."""
        if len(pkt) < 240 or pkt[0] != 1 or pkt[236:240] != DHCP_MAGIC:
            return None, None
        if pkt[24:28] != bytes(4):
            return None, "ignored relayed request"
        opts = parse_dhcp_options(pkt[240:])
        msg = opts.get(53, b"\0")[0]
        mac = pkt[28:28 + min(pkt[2], 16)]
        who = f"{DHCP_NAMES.get(msg, msg)} from {mac.hex(':')}"
        if msg == DISCOVER:
            ip = self.allocate(mac)
            return (self._reply(pkt, OFFER, ip), f"{who} -> OFFER {ip}") if ip else (None, f"{who}: no free address")
        if msg == REQUEST:
            if 54 in opts and opts[54] != self.server_ip.packed:
                return None, f"{who} for another server; ignored"
            raw = opts.get(50) or pkt[12:16]
            wanted = ipaddress.IPv4Address(raw) if len(raw) == 4 else None
            ip = self.allocate(mac)
            if ip is not None and wanted == ip:
                return self._reply(pkt, ACK, ip), f"{who} for {wanted} -> ACK"
            return self._reply(pkt, NAK, None), f"{who} for {wanted} -> NAK (offering {ip})"
        if msg == INFORM:
            return self._reply(pkt, ACK, None), f"{who} -> ACK (settings only)"
        if msg in (RELEASE, DECLINE):
            self.leases.pop(mac, None)
        return None, who


# ------------------------------------------------------------------ service
class OfflineNet:
    """Runs the services on `ip` in background threads. start()/stop() can repeat.

    `state` holds each service's status for display: running, waiting for link,
    stopped, error, blocked (DHCP port owned by another program) or off."""

    def __init__(self, ip: str = DEFAULT_IP, prefix: int = DEFAULT_PREFIX,
                 console_ip: str = DEFAULT_CONSOLE_IP, dhcp: bool = False,
                 log_dir: str | Path | None = None,
                 on_log: Callable[[str], None] | None = None, ports: dict | None = None):
        self.network = validate_addresses(ip, prefix, console_ip)
        self.ip, self.console_ip, self.dhcp = ip, console_ip, dhcp
        self.ports = {"dns": 53, "http": 80, "dhcp": 67, "dhcp_client": 68, **(ports or {})}
        self.log_dir = Path(log_dir) if log_dir else None
        self.on_log = on_log or (lambda line: print(line, flush=True))
        self.stats = dict.fromkeys(("dns", "checks", "rejected", "dhcp"), 0)
        self.state = {"dns": "stopped", "http": "stopped", "dhcp": "stopped" if dhcp else "off"}
        self.log_path: Path | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sockets: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._http: _HTTPServer | None = None
        self._file = None

    # -- helpers
    def log(self, tag: str, message: str) -> None:
        line = f"{dt.datetime.now():%H:%M:%S} {tag:<5} {message}"
        with self._lock:
            if self._file:
                self._file.write(line + "\n")
                self._file.flush()
        self.on_log(line)

    def count(self, key: str) -> None:
        with self._lock:
            self.stats[key] += 1

    def stats_line(self) -> str:
        return " ".join(f"{k}={v}" for k, v in self.stats.items())

    @property
    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def _bind(self, name: str, kind: int, port: int, broadcast: bool = False):
        """Bind to (ip, port). While the address is missing (cable unplugged),
        wait and retry. Returns None if stopped or the port is unavailable."""
        key, waiting = name.lower(), False
        while not self._stop.is_set():
            sock = socket.socket(socket.AF_INET, kind)
            if broadcast:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            try:
                sock.bind((self.ip, port))
            except OSError as exc:
                sock.close()
                if exc.errno == errno.EADDRNOTAVAIL or getattr(exc, "winerror", None) == 10049:
                    if not waiting:
                        self.state[key], waiting = "waiting for link", True
                        self.log(name, f"{self.ip} is not up yet (cable unplugged or console off?); waiting")
                    self._stop.wait(2)
                    continue
                self.state[key] = "blocked" if key == "dhcp" else "error"
                hint = ("another program owns the DHCP port; set the console's IP manually"
                        if key == "dhcp" else "is another copy running?")
                self.log(name, f"cannot use {self.ip}:{port} ({exc}); {hint}")
                return None
            with self._lock:
                self._sockets.append(sock)
            self.state[key] = "running"
            return sock
        return None

    # -- services
    def _serve_udp(self, name: str, port: int, answer, description: str, broadcast=False) -> None:
        sock = self._bind(name, socket.SOCK_DGRAM, port, broadcast)
        if not sock:
            return
        self.log(name, f"listening on {self.ip}:{port} ({description})")
        while not self._stop.is_set():
            try:
                data, peer = sock.recvfrom(1500)
            except ConnectionResetError:  # Windows reports ICMP port-unreachable here
                continue
            except OSError:               # socket closed by stop()
                break
            for reply, addr in answer(data, peer):
                try:
                    sock.sendto(reply, addr)
                except OSError as exc:
                    self.log(name, f"send to {addr[0]} failed: {exc}")
        self.state[name.lower()] = "stopped"

    def _answer_dns(self, data: bytes, peer):
        result = build_dns_reply(data, self.ip)
        if not result:
            return []
        reply, qname, qtype = result
        self.count("dns")
        self.log("DNS", f"{peer[0]} {QTYPE_NAMES.get(qtype, qtype)} {qname[:120]} -> "
                        f"{self.ip if qtype in (1, 255) else 'no records'}")
        return [(reply, peer)]

    def _answer_dhcp(self, data: bytes, _peer):
        reply, text = self._dhcp_server.handle(data)
        if text:
            self.log("DHCP", text)
        if not reply:
            return []
        self.count("dhcp")
        # Send to both broadcast addresses so Windows uses this adapter.
        client = self.ports["dhcp_client"]
        return [(reply, ("255.255.255.255", client)),
                (reply, (str(self.network.broadcast_address), client))]

    def _serve_http(self) -> None:
        sock = self._bind("HTTP", socket.SOCK_STREAM, self.ports["http"])
        if not sock:
            return
        self._http = server = _HTTPServer(sock, self)
        if not self._stop.is_set():
            self.log("HTTP", f"listening on {self.ip}:{self.ports['http']} (connection test only)")
            server.serve_forever(poll_interval=0.3)
        server.server_close()
        self.state["http"] = "stopped"

    # -- control
    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            self.log_path = self.log_dir / f"offline-net-{stamp}.log"
            self._file = open(self.log_path, "a", encoding="utf-8")
        self.log("INFO", f"isolated link {self.network}, this PC {self.ip}, console {self.console_ip}; "
                         "no forwarding, no upstream DNS")
        if self.log_path:
            self.log("INFO", f"log file: {self.log_path}")
        jobs = [lambda: self._serve_udp("DNS", self.ports["dns"], self._answer_dns,
                                        f"every name -> {self.ip}"),
                self._serve_http]
        if self.dhcp:
            self._dhcp_server = DhcpServer(self.ip, self.network, self.console_ip)
            jobs.append(lambda: self._serve_udp("DHCP", self.ports["dhcp"], self._answer_dhcp,
                                                f"console gets {self.console_ip}", broadcast=True))
        self._threads = [threading.Thread(target=job, daemon=True) for job in jobs]
        for t in self._threads:
            t.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._http:  # shutdown() waits for serve_forever, so bound the wait
            closer = threading.Thread(target=self._http.shutdown, daemon=True)
            closer.start()
            closer.join(timeout)
            self._http = None
        with self._lock:
            for sock in self._sockets:
                sock.close()
            self._sockets.clear()
        for t in self._threads:
            t.join(timeout)
        self.log("INFO", f"stopped ({self.stats_line()})")
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None
