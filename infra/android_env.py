"""Prepare ignored Android SDK/Firebase files without loading backend secrets."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ANDROID = ROOT / "android"


def main() -> int:
    sdk = Path(os.environ.get("ANDROID_HOME", "")).expanduser()
    if not os.environ.get("ANDROID_HOME") or not (sdk / "platform-tools" / "adb").is_file():
        print("FAIL: ANDROID_HOME must point to an installed Android SDK")
        return 1
    local = ANDROID / "local.properties"
    if not local.exists():
        escaped = str(sdk.resolve()).replace("\\", "\\\\").replace(":", "\\:")
        local.write_text(f"# Local machine only; do not commit.\nsdk.dir={escaped}\n")
        print("Created ignored android/local.properties")
    else:
        print("Kept existing android/local.properties")

    for variant, package in (
        ("debug", "com.errorfactory.agentping.debug"),
        ("release", "com.errorfactory.agentping"),
    ):
        source = ANDROID / "config" / "firebase" / variant / "google-services.json"
        target = ANDROID / "app" / "src" / variant / "google-services.json"
        candidate = source if source.is_file() else target
        if not candidate.is_file():
            print(f"Firebase {variant}: optional client configuration missing")
            continue
        try:
            config = json.loads(candidate.read_text())
            if "private_key" in config or config.get("type") == "service_account":
                raise ValueError("Not Android client configuration")
            packages = {
                client["client_info"]["android_client_info"]["package_name"]
                for client in config["client"]
            }
            if package not in packages:
                raise ValueError("Package mismatch")
            if not config["project_info"]["project_id"]:
                raise ValueError("Missing Firebase project")
        except (OSError, ValueError, KeyError, TypeError):
            print(f"FAIL: Firebase {variant} client configuration is invalid; contents omitted")
            return 1
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        print(f"Firebase {variant}: package validated; ignored variant configuration ready")
    print("Android SDK/Firebase preparation complete. No backend credentials copied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
