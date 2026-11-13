# Switch 2 Offline Ethernet

[![Build](https://github.com/fernando-granco/switch2-ethernet-offline/actions/workflows/build.yml/badge.svg)](https://github.com/fernando-granco/switch2-ethernet-offline/actions/workflows/build.yml)

Connect a Nintendo Switch 2 straight to a Windows PC with an Ethernet cable. The console reports that it is **online**, but it has **no internet access**.

The PC answers the console's DNS lookups and Nintendo's connection test itself, the same way [90DNS](https://gitlab.com/a/90dns) does, and forwards nothing. You get a private wired network between the PC and the console, for example for homebrew tools that talk to it over the network. Meanwhile the console cannot reach Nintendo's servers or download updates.

Everything works from the **command line**. The **GUI** (`SwitchOfflineEthernet.exe`) is an optional front end that runs the same code.

> Not affiliated with or endorsed by Nintendo. Use at your own risk.

## Requirements

- Windows 10 or 11 with a wired Ethernet port (USB Ethernet adapters work too).
- A Switch 2 in a dock with a LAN port, or with a USB LAN adapter, and an Ethernet cable.
- Administrator rights, to change the adapter's address and firewall rules.
- Command line: Python 3.9 or newer. No other packages are needed. The GUI exe needs no Python.

## Install

Pick one. Each option runs the same code. All but the exe need [Python](https://www.python.org/downloads/) 3.9 or newer and nothing else.

- **GUI (exe)**: download `SwitchOfflineEthernet.exe` from [Releases](https://github.com/fernando-granco/switch2-ethernet-offline/releases). Python is not needed.
- **Command line (zip)**:
  1. Download `switch2-offline-cli-<version>.zip` from [Releases](https://github.com/fernando-granco/switch2-ethernet-offline/releases) and extract it anywhere.
  2. In an administrator terminal, run `switch2-offline.cmd` from that folder in place of `switch2-offline` (in PowerShell, `.\switch2-offline.cmd start`). You can call it from any folder by its full path.
- **pip**: installs the `switch2-offline` and `switch2-offline-gui` commands.
  ```powershell
  pip install git+https://github.com/fernando-granco/switch2-ethernet-offline
  ```
- **From a clone**: run `python -m switch2_offline` in the repo folder. It behaves the same as `switch2-offline`.

## Quick start (command line)

1. Plug the cable into the console's LAN port and the PC's Ethernet port.
2. In an **administrator** terminal, run:
   ```powershell
   switch2-offline start
   ```
   This puts the adapter in offline mode, prints the settings for the console, and starts answering. Leave the window open.
3. On the Switch 2, go to **System Settings › Internet › Internet Settings › Wired Connection › Change Settings** and enter:

   | Field | Value |
   | --- | --- |
   | IP Address Settings | Manual |
   | IP Address | `192.168.77.100` |
   | Subnet Mask | `255.255.255.0` |
   | Gateway | `192.168.77.1` |
   | DNS Settings | Manual: primary `192.168.77.1`, secondary `192.168.77.1` |
   | Proxy | Off |

   Then run **Test Connection**. The terminal shows `HTTP … -> connection check 200`, and the console reports that it is connected.
4. Programs on the PC can now reach the console at `192.168.77.100`.
5. Press **Ctrl+C** to stop. The adapter goes back to automatic IP.

You only need to enter the console settings once. To use other networks on the console again, set it back to **Automatic**.

## Commands

| Command | What it does | Admin |
| --- | --- | --- |
| `start` | Puts the adapter in offline mode (if it isn't already), runs the services, and restores the adapter on Ctrl+C. Add `--keep` to leave it in offline mode. | yes |
| `serve` | Runs the services only. The adapter must already be in offline mode. | no |
| `setup` | Puts the adapter in offline mode and leaves it there. | yes |
| `restore` | Undoes `setup`: automatic IP and DNS again, firewall rules removed. | yes |
| `status` | Shows the link, mode, addresses, firewall rules, whether the console answers ping, and the isolation check. | no |
| `adapters` | Lists wired adapters and marks the one in offline mode. | no |
| `gui` | Opens the control window. Same options as `start`. | yes, for Start/Restore |

Options (run `switch2-offline <command> -h` for details):

| Option | Default | Commands |
| --- | --- | --- |
| `-a`, `--adapter NAME` | The adapter already in offline mode, else `Ethernet`, else the only wired adapter | start, setup, restore, status, gui |
| `--ip` | `192.168.77.1` (this PC) | all |
| `--console-ip` | `192.168.77.100` | all |
| `--prefix` | `24` | all |
| `--dhcp` | off | start, serve, gui |
| `--log-dir` | `%LOCALAPPDATA%\switch2-ethernet-offline\logs` | start, serve, gui |
| `--keep` | off | start |

Examples:

```powershell
switch2-offline start -a "Ethernet 2"          # a different adapter
switch2-offline start --keep                   # stay in offline mode after Ctrl+C
switch2-offline setup; switch2-offline serve   # set up once, then serve
switch2-offline restore                        # back to normal later
switch2-offline status                         # check everything (no admin needed)
```

## The GUI

Run `SwitchOfflineEthernet.exe` (or `switch2-offline gui`) and accept the administrator prompt. The exe accepts the same options as `gui`, for example `SwitchOfflineEthernet.exe --adapter "Ethernet 2"`.

| Part | Meaning |
| --- | --- |
| **▶ Start offline mode** | Same as `start`: sets up the adapter if needed, then runs the services. |
| **■ Stop & restore Ethernet** | Stops the services and runs `restore`. |
| **Also run DHCP** | Same as `--dhcp`, for the next start. |
| **Wired adapter** | The port the console is plugged into. Locked while in use. |
| **Cable** | Link state and speed. |
| **PC IP** | *offline mode*, *normal*, or *partly configured* (press Start or Stop to fix). |
| **Firewall** | This tool's inbound rules (n/4). |
| **Server** | DNS / HTTP / DHCP: `running`, `waiting for link`, `stopped`, `error`, `blocked` or `off`. |
| **Isolation** | Re-checked every 10 s while the server runs. Red **NOT ISOLATED** means the console could reach the internet through this PC; a warning window explains why. |
| **Console** | Whether the console answers ping. |
| **Activity** | DNS answers, connection checks, rejected requests, DHCP replies, and the time of the last check. |
| **Start / Stop server only** | Runs or stops the services without touching the adapter (like `serve`). |

When you close the window, it asks whether to restore the adapter. **No** leaves the adapter in offline mode (like `--keep`).

## How it works

| Part | Behaviour |
| --- | --- |
| Adapter | Static `192.168.77.1/24` with **no gateway** and no DNS servers. Your other connections (e.g. Wi-Fi) are untouched. |
| DNS (udp/53) | Every name resolves to the PC. Nothing is forwarded upstream. |
| HTTP (tcp/80) | The connection-test hosts (`ctest.cdn.nintendo.net`, `ctest.p01.ctest.srv.nintendo.net`, `conntest.nintendowifi.net`, …) get `200` + `X-Organization: Nintendo`. Every other request gets `404`. |
| DHCP (udp/67) | Optional. Gives the console `--console-ip`, with the PC as gateway and DNS. |
| Firewall | Four inbound rules (DNS, HTTP, DHCP, ping) on that adapter only, in the group `Switch2 Offline Ethernet`. |

**Why the console stays offline:**
- The adapter has no gateway.
- `setup` **refuses to run** if any of these is true. With all of them off, Windows never forwards the console's packets.
  - Windows IP routing is on.
  - IPv4 or IPv6 forwarding is on for any adapter.
  - Internet Connection Sharing is on.
  - A Windows NAT covers the subnet.
  - Another network already uses the same subnet.
- **While the services run**, the same checks repeat every 10 seconds, together with a check that the adapter has no gateway. This catches a change made after Start, such as turning on sharing or a VPN that routes for other devices. Any problem is logged as `WARN NOT ISOLATED: …`. The GUI also turns the **Isolation** row red and shows a warning.
- The tool forwards nothing itself. DNS answers come from the PC, and HTTP answers only the connection test. Nothing listens on HTTPS (443) or any other port.

The "online" result is local only.

**On the console:** a Switch 2 with saved Wi-Fi networks can join one of them if the wired link drops, for example when it is undocked. To be sure it stays offline, delete its saved Wi-Fi networks under **System Settings › Internet**.

## DHCP

A manual console IP is the reliable choice, so DHCP is off by default. On Windows the DHCP server port is often owned by other software, such as security suites, Internet Connection Sharing or virtualization tools. If so, `--dhcp` logs `cannot use … :67` and the other services keep running.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Console: **2110-2963 Unable to obtain IP address** | The console is set to Automatic. Enter the manual settings, or try `--dhcp`. |
| `error: administrator rights are needed` | Open the terminal with *Run as administrator*. |
| `refusing to set up: …` | Do what the message says (turn off routing/forwarding/ICS, remove the NAT, or disconnect the network that uses the same subnet). |
| `WARN NOT ISOLATED: …` while running | Something changed after Start. Undo it (the message says what), or stop until it is fixed. |
| `waiting for link` doesn't clear | Cable unplugged, console/dock off, or the wrong adapter. Check `switch2-offline adapters`. |
| `cannot use … :53` / `:80` | Another copy is running, or another program uses that port. |
| No log lines and no ping reply | Third-party firewall or security software may block the new network. Mark it as trusted/local, or allow the program. |
| A VPN is connected | Its kill switch or LAN blocking can drop local traffic. Disconnect it or allow LAN access. |
| The test fails at a later step (NAT type, speed) | Only the basic connection test is answered. The log shows which host was asked for next. |
| Adapter left in offline mode after a crash | `switch2-offline restore` (or **■ Stop & restore Ethernet**). |

## Development

```powershell
python -m unittest -v               # tests: no admin and no console needed
python -m pip install pyinstaller
.\packaging\build_exe.ps1           # builds dist\SwitchOfflineEthernet.exe
python packaging\build_zip.py       # builds dist\switch2-offline-cli-<version>.zip
```

| Path | Contents |
| --- | --- |
| `switch2_offline/services.py` | DNS, HTTP and DHCP (`OfflineNet`). Standard library only. |
| `switch2_offline/windows.py` | Adapter setup/restore/status through PowerShell, plus the isolation checks. |
| `switch2_offline/cli.py` | The commands. |
| `switch2_offline/gui.py` | The tkinter window, built on the same functions. |
| `packaging/` | Build scripts for the exe (PyInstaller) and the command-line zip, and the zip's `switch2-offline.cmd` launcher. |
| `.github/workflows/build.yml` | Runs the tests and builds the exe and the zip on every push. Pushing a `v*` tag attaches both to that release. |

## Credits and license

The connection-test hosts and responses are adapted from [90DNS](https://gitlab.com/a/90dns) by Ave (revision [`4d477d71`](https://gitlab.com/a/90dns/-/tree/4d477d7181188690203aae5f4e56e287edd21734)).

Licensed under the [GNU General Public License v2.0 or later](LICENSE).
