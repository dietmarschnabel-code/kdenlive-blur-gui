#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' kdenlive_blur_gui.py)"
[ -n "$VERSION" ] || { echo "Could not read the app version from kdenlive_blur_gui.py" >&2; exit 1; }
command -v makensis >/dev/null 2>&1 || { echo "makensis is required (install the nsis package)." >&2; exit 1; }

mkdir -p dist
makensis -DMyAppVersion="$VERSION" packaging/windows/kdenlive-blur-gui.nsi
echo "Built dist/kdenlive-blur-gui-setup-${VERSION}.exe"