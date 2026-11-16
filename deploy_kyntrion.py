"""Install the KYNTRION extension in a running PiarBot checkout on Termux."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


SOURCE = Path(__file__).resolve().parent
# go_gigi PiarBot main 55a8a17, checked against the running Realme copy.
BASE = {"bot.py": "4a494d44aa3bb38642f0aa35b3963e63fcd4063cd6f4a7fdcdfc23c07dc37f82",
        "keyboards.py": "8a8549f92ecc48c8ccef8faa76c01867423ca5c639a071d06f532926ac4883c3"}


def pid(root):
    try:
        number = int((root / "data/bot.pid").read_text())
        os.kill(number, 0)
        return number if number > 1 else None
    except (ValueError, OSError):
        return None


def restart(root):
    subprocess.run(["bash", "run.sh", "restart"], cwd=root, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25)


def log_contains(root, offset, marker):
    with (root / "logs/bot.log").open("rb") as fp:
        fp.seek(offset)
        return marker.encode() in fp.read()


def deploy(root):
    if not pid(root):
        raise RuntimeError("PiarBot is not running in the specified checkout")
    for name, digest in BASE.items():
        if sha256((root / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Live {name} differs from the verified base")
    if (root / "kyntrion_tools").exists():
        raise RuntimeError("The extension directory already exists")
    subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-q"],
                   cwd=SOURCE, check=True)
    backup = Path(tempfile.mkdtemp(prefix="kyntrion-release-", dir=root))
    os.chmod(backup, 0o700)
    for name in BASE:
        shutil.copy2(root / name, backup / name)
    shutil.copytree(SOURCE / "kyntrion_tools", backup / "new_tools",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in BASE:
        shutil.copy2(SOURCE / name, backup / ("new_" + name))
    log = root / "logs/bot.log"
    offset = log.stat().st_size if log.exists() else 0
    os.replace(backup / "new_tools", root / "kyntrion_tools")
    try:
        for name in BASE:
            os.replace(backup / ("new_" + name), root / name)
        restart(root)
        for _ in range(25):
            if pid(root) and log_contains(root, offset, "KYNTRION ready group=157735873"):
                print(json.dumps({"service": "PiarBot-VK", "group": 157735873, "long_poll": "ready",
                                  "pid": pid(root), "backup": backup.name}))
                return
            time.sleep(1)
        raise RuntimeError("No startup receipt from go_gigi")
    except Exception:
        for name in BASE:
            shutil.copy2(backup / name, root / name)
        os.replace(root / "kyntrion_tools", backup / "unloaded_tools")
        restart(root)
        print("Restored previous PiarBot files", file=sys.stderr)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    args = parser.parse_args()
    deploy(args.checkout.resolve(strict=True))
