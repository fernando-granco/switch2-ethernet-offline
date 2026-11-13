# SPDX-License-Identifier: GPL-2.0-or-later
"""Build dist/switch2-offline-cli-<version>.zip: the command line without the exe.

It holds the switch2_offline package, the switch2-offline.cmd launcher, the
README and the license, all in one folder. Needs only Python 3.9+ to run.
"""
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from switch2_offline import __version__  # noqa: E402


def main() -> Path:
    folder = f"switch2-offline-{__version__}"
    files = {f"{folder}/switch2-offline.cmd": ROOT / "packaging" / "switch2-offline.cmd",
             f"{folder}/README.md": ROOT / "README.md",
             f"{folder}/LICENSE": ROOT / "LICENSE"}
    for path in sorted((ROOT / "switch2_offline").iterdir()):
        if path.suffix in (".py", ".ico"):
            files[f"{folder}/switch2_offline/{path.name}"] = path
    out = ROOT / "dist" / f"switch2-offline-cli-{__version__}.zip"
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, path in files.items():
            if name.endswith(".cmd"):  # cmd.exe wants CRLF whatever the checkout has
                lines = path.read_text(encoding="ascii").splitlines()
                zf.writestr(name, "\r\n".join(lines) + "\r\n")
            else:
                zf.write(path, name)
    print(f"{out.name}  {out.stat().st_size:,} bytes, {len(files)} files")
    return out


if __name__ == "__main__":
    main()
