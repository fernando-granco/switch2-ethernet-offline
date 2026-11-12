"""Command-line parsing and the pure decision logic in switch2_offline.windows."""
import contextlib
import io
import ipaddress
import os
import unittest

from switch2_offline import cli, console_settings, validate_addresses
from switch2_offline.windows import AdapterState, SetupError, choose_adapter, find_isolation_problems

NET = ipaddress.IPv4Network("192.168.77.0/24")
CLEAN = {"router": 0, "forwarding": [], "ics": [], "addresses": [{"alias": "Wi-Fi", "ip": "10.0.0.5"}]}


class Parser(unittest.TestCase):
    def parse(self, *argv):
        return cli.build_parser().parse_args(list(argv))

    def test_defaults(self):
        args = self.parse("start")
        self.assertEqual((args.ip, args.console_ip, args.prefix), ("192.168.77.1", "192.168.77.100", 24))
        self.assertFalse(args.dhcp or args.keep)
        self.assertIsNone(args.adapter)

    def test_every_command_parses(self):
        for command in cli.COMMANDS:
            self.assertEqual(self.parse(command).command, command)

    def test_options(self):
        args = self.parse("start", "-a", "Ethernet 2", "--ip", "10.77.0.1", "--console-ip",
                          "10.77.0.2", "--dhcp", "--keep")
        self.assertEqual((args.adapter, args.ip, args.console_ip), ("Ethernet 2", "10.77.0.1", "10.77.0.2"))
        self.assertTrue(args.dhcp and args.keep)

    def test_bad_console_address_is_rejected(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["status", "--console-ip", "10.0.0.2"])


class Addresses(unittest.TestCase):
    def test_validate(self):
        self.assertEqual(validate_addresses("192.168.77.1", 24, "192.168.77.100"), NET)
        for ip, console in (("8.8.8.1", "8.8.8.2"), ("192.168.77.1", "192.168.77.1")):
            with self.assertRaises(ValueError):
                validate_addresses(ip, 24, console)

    def test_console_settings(self):
        settings = dict(console_settings("10.77.0.1", "10.77.0.2", 16))
        self.assertEqual(settings["Subnet Mask"], "255.255.0.0")
        self.assertEqual(settings["Gateway"], "10.77.0.1")


class AdapterChoice(unittest.TestCase):
    def test_order_of_preference(self):
        self.assertEqual(choose_adapter("X", ["Ethernet"], None), "X")
        self.assertEqual(choose_adapter(None, ["Ethernet", "Ethernet 2"], "Ethernet 2"), "Ethernet 2")
        self.assertEqual(choose_adapter(None, ["Ethernet 2", "Ethernet"], None), "Ethernet")
        self.assertEqual(choose_adapter(None, ["USB LAN"], None), "USB LAN")

    def test_ambiguous_or_missing(self):
        with self.assertRaises(SetupError):
            choose_adapter(None, ["LAN 1", "LAN 2"], None)
        with self.assertRaises(SetupError):
            choose_adapter(None, [], None)

    def test_mode(self):
        offline = AdapterState("Ethernet", True, True, "Up", "1 Gbps", False, ["192.168.77.1"], 4)
        normal = AdapterState("Ethernet", True, True, "Up", "1 Gbps", True, ["10.0.0.9"], 0)
        partial = AdapterState("Ethernet", True, True, "Up", "1 Gbps", False, ["192.168.77.1"], 0)
        self.assertEqual([s.mode("192.168.77.1") for s in (offline, normal, partial)],
                         ["offline", "normal", "partial"])


class Isolation(unittest.TestCase):
    def problems(self, **changes):
        return find_isolation_problems({**CLEAN, **changes}, "Ethernet", NET)

    def test_clean_machine(self):
        self.assertEqual(self.problems(), [])

    def test_each_problem_is_reported(self):
        self.assertIn("routing", self.problems(router=1)[0])
        self.assertIn("Wi-Fi", self.problems(forwarding="Wi-Fi")[0])  # one-item JSON arrays are scalars
        self.assertIn("Sharing is on", self.problems(ics=["Wi-Fi"])[0])
        self.assertIn("could not check", self.problems(ics=None)[0])

    def test_subnet_clash_ignores_own_adapter(self):
        addresses = [{"alias": "Ethernet", "ip": "192.168.77.1"}, {"alias": "Wi-Fi", "ip": "192.168.77.5"}]
        problems = self.problems(addresses=addresses)
        self.assertEqual(len(problems), 1)
        self.assertIn("Wi-Fi", problems[0])
        self.assertNotIn("Ethernet,", problems[0])


@unittest.skipUnless(os.name == "nt", "the GUI and its status queries are Windows-only")
class GuiSmoke(unittest.TestCase):
    def test_window_opens_and_closes(self):
        os.environ["SWITCH2_OFFLINE_SMOKE_TEST"] = "1500"
        try:
            self.assertEqual(cli.main(["gui"]), 0)
        finally:
            del os.environ["SWITCH2_OFFLINE_SMOKE_TEST"]


if __name__ == "__main__":
    unittest.main()
