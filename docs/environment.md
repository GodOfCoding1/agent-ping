# Environment inventory

Configured on 2026-10-28, Asia/Kolkata. Host: macOS 26.6.2 arm64, 16 GiB RAM.

| Tool | Verified version/location | Treatment |
| --- | --- | --- |
| Git | 2.50.1, `/usr/bin/git` | Reused |
| Homebrew | 7.0.11, `/opt/homebrew/bin/brew` | Reused |
| Python | 3.13.18, `/opt/homebrew/bin/python3.13` | Installed |
| uv | 0.13.5, `/opt/homebrew/bin/uv` | Installed |
| Node/npm | 24.5.0 / 11.5.1, `/opt/homebrew/bin` | Reused |
| JDK | Android Studio OpenJDK 21.0.10 | Reused through private symlink |
| Android command-line tools | 23.0, native macOS arm64 | Installed in canonical SDK location |
| Android CLI | 1.1.16717390 | Bundled with current command-line tools |
| Platform Tools / ADB | 37.0.0 / 1.0.41 | Reused |
| Emulator | 36.5.11 | Reused; acceleration and boot tested |
| System image | API 36 Google Play arm64-v8a, revision 7 | Installed |
| AVD | AgentPing_API36, Pixel 8 | Created |
| SDK platforms | 34, 35, 36, 36.1 | Reused |
| Build Tools | 36.0.0, 36.1.0, 37.0.0 | Reused |
| Docker Desktop / CLI | 4.69.0 / 29.4.0 | Existing engine started |
| Docker Compose | 5.1.1 | Reused |
| PostgreSQL clients | libpq / psql 18.6 | Installed; no local DB server |
| curl | 8.7.1 | Reused |
| GitHub CLI | 2.103.2 | Reused; login deferred |
| gcloud | 590.0.1 | Installed and authenticated |
| Neon CLI | 8.13.1 | Installed; login stored in OS Keychain |
| Firebase CLI | 15.34.1 | Installed and authenticated |
| MCP Inspector | 2.12.0 | Pinned npx invocation |
| Railway | Not installed | Excluded by user |

Backend environment: `backend/.venv`, exact resolution in `backend/uv.lock`.
The application package lives in `backend/src/agentping`. Principal dependencies:
FastAPI 0.162.0, MCP 2.3.0, SQLAlchemy 2.1.6, psycopg 3.3.6, Alembic 1.20.1,
Pydantic 2.14.1, Firebase Admin 7.7.0, Uvicorn 0.54.0, pytest 9.2.0, Ruff 0.17.3
and mypy 1.20.2.

## Host configuration

`~/.config/agentping/env.sh` contains only SDK/JDK/PATH configuration. It is sourced
from `.zshrc` and `.zprofile`; originals are backed up under the same private
configuration directory. `./dev` also sources it explicitly, so tool execution does
not rely on an interactive shell loading startup files.

- `ANDROID_HOME=$HOME/Library/Android/sdk`
- `JAVA_HOME=$HOME/.local/share/agentping/jdk`
- JDK symlink targets Android Studio's bundled runtime.
- PATH includes Java, canonical command-line tools, ADB, emulator, Homebrew and libpq.

The JDK symlink avoids an unquoted-path issue in older SDK launchers. Previous
command-line-tools 19/20 directories are preserved under
`~/.local/share/agentping/sdk-backups/`, outside SDK package discovery.

Current Google SDK tooling deprecates `sdkmanager` in favor of `android sdk`.
Its informational deprecation notice is expected. `avdmanager` remains usable.
No NDK, CMake, standalone Gradle, Kotlin compiler or second Android Studio was added.

Gcloud's installation initially omitted its managed Python environment, causing
Cloud Run worker commands to fail importing `grpc`. It was repaired using
`gcloud config virtualenv create --python-to-use=/opt/homebrew/bin/python3.13 --enable-virtualenv`.
Gcloud now runs in `~/.config/gcloud/virtenv`; no global pip or sudo pip was used.

## Compatibility references

- https://developer.android.com/build/releases/agp-9-2-0-release-notes
- https://developer.android.com/build/releases/about-agp
- https://docs.gradle.org/current/userguide/compatibility.html
- https://developer.android.com/tools/agents/android-cli
- https://py.sdk.modelcontextprotocol.io/
- https://docs.astral.sh/uv/guides/projects/

The initial Android build baseline is AGP 9.2.x / Gradle wrapper 9.4.1 / JDK 21 /
API 36. Revalidate stable Kotlin/Compose/KSP versions when the Android app is built.
