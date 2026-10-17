#!/usr/bin/env python3
"""Cross-platform launcher: native Linux, WSL2, and native Windows all run
this the same way (`python run.py` / `./run.sh` / `./run.ps1`)."""
import os
import platform

import uvicorn


def describe_env() -> str:
    if platform.system() == "Windows":
        return "windows"
    try:
        with open("/proc/sys/kernel/osrelease") as f:
            if "microsoft" in f.read().lower():
                return "wsl2"
    except OSError:
        pass
    return "linux"


def main() -> None:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    port = int(os.environ.get("SWITCHBOARD_PORT", "8787"))

    env = describe_env()
    if env == "wsl2":
        print(f"Detected WSL2. Open http://localhost:{port} from your Windows browser.")
    else:
        print(f"Open http://localhost:{port} in your browser.")

    # 127.0.0.1 only: reachable from this machine (and, on WSL2, from
    # Windows via its automatic localhost forwarding), never the network.
    uvicorn.run("app:app", host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
