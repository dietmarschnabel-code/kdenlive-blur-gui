#!/bin/sh
# Builds an RPM on Fedora:   sudo dnf install rpm-build desktop-file-utils
#                            ./packaging/linux/build-rpm.sh
# Result: ~/rpmbuild/RPMS/noarch/kdenlive-blur-gui-<version>-1.<dist>.noarch.rpm
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' kdenlive_blur_gui.py)"
[ -f LICENSE ] || { echo "LICENSE is missing - add the GPL-3.0 text first (see README)."; exit 1; }
mkdir -p "$HOME/rpmbuild/SOURCES"
TARBALL="$HOME/rpmbuild/SOURCES/kdenlive-blur-gui-$VERSION.tar.gz"
tar --exclude=.git --exclude=build --exclude=dist \
    --transform "s,^\.,kdenlive-blur-gui-$VERSION," -czf "$TARBALL" .
sed "s/^Version:.*/Version:        $VERSION/" packaging/linux/kdenlive-blur-gui.spec > /tmp/kdenlive-blur-gui.spec
rpmbuild -ba /tmp/kdenlive-blur-gui.spec
