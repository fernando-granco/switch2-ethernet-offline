"""DNS/HTTP/DHCP behaviour, using loopback and high ports (no admin, no console)."""
import ipaddress
import socket
import struct
import time
import unittest
import urllib.error
import urllib.request

from switch2_offline import services

NET = ipaddress.IPv4Network("192.168.77.0/24")
MAC = bytes.fromhex("020000000001")  # locally administered dummy address


def dns_query(name, qtype=1):
    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.split("."))
    return struct.pack("!6H", 0x1234, 0x0100, 1, 0, 0, 0) + labels + b"\0" + struct.pack("!HH", qtype, 1)


def dhcp_packet(msg_type, options=b""):
    hdr = bytearray(236)
    hdr[0:3] = b"\x01\x01\x06"
    hdr[4:8] = b"\xde\xad\xbe\xef"
    hdr[28:34] = MAC
    return bytes(hdr) + services.DHCP_MAGIC + bytes([53, 1, msg_type]) + options + b"\xff"


def ip_option(code, ip):
    return bytes([code, 4]) + socket.inet_aton(ip)


class Dns(unittest.TestCase):
    def test_a_record_points_to_pc(self):
        reply, name, qtype = services.build_dns_reply(dns_query("CTest.Cdn.Nintendo.net"), "192.168.77.1")
        self.assertEqual((name, qtype), ("ctest.cdn.nintendo.net", 1))
        tid, flags, _, answers = struct.unpack("!4H", reply[:8])
        self.assertEqual((tid, answers), (0x1234, 1))
        self.assertTrue(flags & 0x8000)
        self.assertEqual(reply[-4:], socket.inet_aton("192.168.77.1"))

    def test_aaaa_is_empty_noerror(self):
        reply, _, _ = services.build_dns_reply(dns_query("example.com", 28), "192.168.77.1")
        self.assertEqual(struct.unpack("!H", reply[6:8])[0], 0)
        self.assertEqual(reply[3] & 0x0F, 0)

    def test_malformed_is_ignored(self):
        self.assertIsNone(services.build_dns_reply(b"\0" * 5, "192.168.77.1"))
        self.assertIsNone(services.build_dns_reply(dns_query("x")[:14], "192.168.77.1"))


class Dhcp(unittest.TestCase):
    def setUp(self):
        self.server = services.DhcpServer("192.168.77.1", NET, "192.168.77.100")

    def test_discover_then_request(self):
        offer, _ = self.server.handle(dhcp_packet(services.DISCOVER))
        self.assertEqual(offer[16:20], socket.inet_aton("192.168.77.100"))
        opts = services.parse_dhcp_options(offer[240:])
        self.assertEqual(opts[53], bytes([services.OFFER]))
        self.assertEqual(opts[1], socket.inet_aton("255.255.255.0"))
        self.assertEqual(opts[3], socket.inet_aton("192.168.77.1"))
        self.assertEqual(opts[6], socket.inet_aton("192.168.77.1"))
        request = ip_option(50, "192.168.77.100") + ip_option(54, "192.168.77.1")
        ack, _ = self.server.handle(dhcp_packet(services.REQUEST, request))
        self.assertEqual(services.parse_dhcp_options(ack[240:])[53], bytes([services.ACK]))

    def test_old_address_gets_nak(self):
        nak, text = self.server.handle(dhcp_packet(services.REQUEST, ip_option(50, "192.168.77.42")))
        self.assertIn("NAK", text)
        self.assertEqual(services.parse_dhcp_options(nak[240:])[53], bytes([services.NAK]))

    def test_request_for_other_server_is_ignored(self):
        reply, text = self.server.handle(dhcp_packet(services.REQUEST, ip_option(54, "10.0.0.1")))
        self.assertIsNone(reply)
        self.assertIn("another server", text)


class OfflineNetLoopback(unittest.TestCase):
    PORTS = {"dns": 15353, "http": 18080}

    def make(self, **kw):
        return services.OfflineNet("127.0.0.1", 8, "127.0.0.100", on_log=lambda _line: None,
                                   ports=self.PORTS, **kw)

    def wait_running(self, net):
        deadline = time.time() + 3
        while net.state["http"] != "running" and time.time() < deadline:
            time.sleep(0.05)

    def http(self, host):
        req = urllib.request.Request(f"http://127.0.0.1:{self.PORTS['http']}/", headers={"Host": host})
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status, r.headers.get("X-Organization"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("X-Organization"), e.read()

    def test_dns_and_connection_test(self):
        net = self.make()
        net.start()
        try:
            self.wait_running(net)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(2)
                s.sendto(dns_query("conntest.nintendowifi.net"), ("127.0.0.1", self.PORTS["dns"]))
                self.assertEqual(s.recvfrom(512)[0][-4:], socket.inet_aton("127.0.0.1"))
            self.assertEqual(self.http("ctest.p01.ctest.srv.nintendo.net"), (200, "Nintendo", b"ok"))
            status, org, body = self.http("conntest.nintendowifi.net:80")
            self.assertEqual((status, org), (200, "Nintendo"))
            self.assertIn(b"This is test.html page", body)
            self.assertEqual(self.http("example.com")[:2], (404, None))
            self.assertEqual((net.stats["dns"], net.stats["checks"], net.stats["rejected"]), (1, 2, 1))
        finally:
            net.stop()

    def test_stop_and_restart(self):
        for _ in range(2):
            net = self.make()
            net.start()
            self.wait_running(net)
            t0 = time.time()
            net.stop()
            self.assertLess(time.time() - t0, 2.5)
            self.assertFalse(net.running)

    def test_stop_while_waiting_for_link(self):
        net = services.OfflineNet("10.255.254.1", 24, "10.255.254.100", dhcp=True,
                                  on_log=lambda _line: None)  # address not on this machine
        net.start()
        time.sleep(0.5)
        self.assertEqual(net.state["dns"], "waiting for link")
        net.stop()
        self.assertFalse(net.running)


if __name__ == "__main__":
    unittest.main()
