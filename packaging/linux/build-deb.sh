#!/bin/sh
# Builds dist/kdenlive-blur-gui_<version>_all.deb  (run from anywhere; needs dpkg-deb)
#   MAINTAINER="Dietmar Schnabel <dietmar.schnabel<at>gmail.com>" ./packaging/linux/build-deb.sh
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' kdenlive_blur_gui.py)"
MAINTAINER="${MAINTAINER:-Dietmar Schnabel <dietmar.schnabel<at>gmail.com>}"
HOMEPAGE="${HOMEPAGE:-https://github.com/YOUR-USER/kdenlive-blur-gui}"
PKG="kdenlive-blur-gui"
STAGE="build/deb/${PKG}_${VERSION}_all"

rm -rf "$STAGE"
install -d "$STAGE/DEBIAN" "$STAGE/usr/bin" "$STAGE/usr/share/$PKG" "$STAGE/usr/share/applications" "$STAGE/usr/share/doc/$PKG"

install -m 755 packaging/linux/kdenlive-blur-gui          "$STAGE/usr/bin/$PKG"
install -m 644 kdenlive_blur_gui.py                       "$STAGE/usr/share/$PKG/kdenlive_blur_gui.py"
install -m 644 kdenlive-blur-gui.png                      "$STAGE/usr/share/$PKG/kdenlive-blur-gui.png"
install -m 644 packaging/linux/kdenlive-blur-gui.desktop  "$STAGE/usr/share/applications/$PKG.desktop"
for s in 512 256 128 64 48; do
    install -D -m 644 "assets/icons/$s.png" "$STAGE/usr/share/icons/hicolor/${s}x${s}/apps/autoblur-gui.png"
done
gzip -9n -c README.md > "$STAGE/usr/share/doc/$PKG/README.md.gz"
cat > "$STAGE/usr/share/doc/$PKG/copyright" <<COPYRIGHT
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: $PKG
Source: $HOMEPAGE

Files: *
Copyright: 2026 Kdenlive Auto-Blur Tool contributors
License: AGPL-3+
 This program is free software: you can redistribute it and/or modify it under the
 terms of the GNU Affero General Public License as published by the Free Software
 Foundation, either version 3 of the License, or (at your option) any later version.
 .
 On Debian systems the full text is in /usr/share/common-licenses/AGPL-3.
COPYRIGHT

INSTALLED_KB="$(du -sk "$STAGE/usr" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: $PKG
Version: $VERSION
Section: video
Priority: optional
Architecture: all
Installed-Size: $INSTALLED_KB
Depends: python3 (>= 3.10), python3-tk, python3-venv
Recommends: ffmpeg
Suggests: kdenlive, intel-opencl-icd
Maintainer: $MAINTAINER
Homepage: $HOMEPAGE
Description: detect, track and blur people and vehicles in Kdenlive projects
 Opens an already cut Kdenlive project, finds people and vehicles in the parts
 of the video that are really used on the timeline (YOLO), and writes a copy
 of the project with tracking blur effects added. A check video can also be
 rendered directly.
 .
 On first start the tool offers to download its Python components (PyTorch,
 Ultralytics, OpenCV) into a private folder in your home directory.
CONTROL

cat > "$STAGE/DEBIAN/postinst" <<'POSTINST'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
fi
exit 0
POSTINST
cp "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"
sed -i 's/"\$1" = "configure"/"$1" = "remove" ] || [ "$1" = "purge"/' "$STAGE/DEBIAN/postrm"
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"

mkdir -p dist
dpkg-deb --root-owner-group --build "$STAGE" "dist/${PKG}_${VERSION}_all.deb"
echo "Built dist/${PKG}_${VERSION}_all.deb"
