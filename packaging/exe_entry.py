# SPDX-License-Identifier: GPL-2.0-or-later
"""PyInstaller entry point. The exe opens the control window and accepts the
same options as `switch2-offline gui` (for example --adapter or --ip)."""
from switch2_offline.cli import gui_main

gui_main()
