#!/usr/bin/env bash
# Build installable packages for Kora.
#
#   ./build-packages.sh          build both a .deb and an .rpm
#   ./build-packages.sh deb      Ubuntu / Debian package only
#   ./build-packages.sh rpm      Fedora package only
#
# Output goes to ./dist. The packages are architecture independent and can be
# built on either distribution:
#
#   Fedora:  sudo dnf install rpm-build dpkg
#   Ubuntu:  sudo apt install rpm            (dpkg-deb is already there)
#
# Optional environment:
#   MAINTAINER="Your Name <you@example.com>"   recorded in the packages
#   RELEASE=2                                  package revision (default 1)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

NAME=kora
APP_ID=app.kora.Kora
VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' kora/__init__.py)"
RELEASE="${RELEASE:-1}"
MAINTAINER="${MAINTAINER:-Kora maintainers <noreply@example.com>}"
SUMMARY="Browse and play music from Lyrion Music Server"
OUT="$ROOT/dist"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

[ -n "$VERSION" ] || { echo "Could not read the version from kora/__init__.py" >&2; exit 1; }

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
need() {
    command -v "$1" >/dev/null 2>&1 && return 0
    echo "Missing '$1'. Install it with:  $2" >&2
    return 1
}

# Lay the files out exactly as they will sit on the target system.
stage() {
    local dest="$1"
    install -d "$dest/usr/share/$NAME/kora"
    install -m 644 kora/*.py "$dest/usr/share/$NAME/kora/"
    install -D -m 755 packaging/kora-launcher "$dest/usr/bin/$NAME"
    install -D -m 644 "data/$APP_ID.desktop" "$dest/usr/share/applications/$APP_ID.desktop"
    install -D -m 644 "data/$APP_ID.metainfo.xml" "$dest/usr/share/metainfo/$APP_ID.metainfo.xml"
    install -D -m 644 "data/icons/hicolor/scalable/apps/$APP_ID.svg" \
        "$dest/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg"
    install -D -m 644 "data/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg" \
        "$dest/usr/share/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg"
    install -D -m 644 "data/icons/hicolor/256x256/apps/$APP_ID.png" \
        "$dest/usr/share/icons/hicolor/256x256/apps/$APP_ID.png"
    install -D -m 644 README.md "$dest/usr/share/doc/$NAME/README.md"
}

build_deb() {
    need dpkg-deb "sudo dnf install dpkg   (Fedora)  or  sudo apt install dpkg   (Ubuntu)"
    say "Building .deb"
    local pkg="$WORK/deb"
    stage "$pkg"
    # Debian policy: licence summary in the doc directory, full text is in
    # /usr/share/common-licenses on every Debian/Ubuntu system.
    cat > "$pkg/usr/share/doc/$NAME/copyright" <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: Kora

Files: *
Copyright: $(date +%Y) $MAINTAINER
License: GPL-3+
 This program is free software: you can redistribute it and/or modify it
 under the terms of the GNU General Public License as published by the Free
 Software Foundation, either version 3 of the License, or (at your option)
 any later version.
 .
 On Debian systems the full text of the GNU General Public License version 3
 can be found in /usr/share/common-licenses/GPL-3.
EOF
    install -d "$pkg/DEBIAN"
    local size
    size="$(du -sk --exclude=DEBIAN "$pkg" | cut -f1)"
    cat > "$pkg/DEBIAN/control" <<EOF
Package: $NAME
Version: $VERSION-$RELEASE
Section: sound
Priority: optional
Architecture: all
Maintainer: $MAINTAINER
Installed-Size: $size
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0 (>= 4.12), gir1.2-adw-1 (>= 1.5), gir1.2-gstreamer-1.0, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good
Recommends: gstreamer1.0-libav, squeezelite
Description: $SUMMARY
 Kora is a GTK4 and libadwaita client for Lyrion Music Server (formerly
 Logitech Media Server). Browse the library, radio, apps and favorites,
 control and sync every player on the network, and listen on this computer
 through the built-in output or a managed Squeezelite.
EOF
    mkdir -p "$OUT"
    local file="$OUT/${NAME}_${VERSION}-${RELEASE}_all.deb"
    dpkg-deb --root-owner-group --build "$pkg" "$file" >/dev/null
    echo "    $file"
}

build_rpm() {
    need rpmbuild "sudo dnf install rpm-build   (Fedora)  or  sudo apt install rpm   (Ubuntu)"
    say "Building .rpm"
    local stage_dir="$WORK/rpm-stage" top="$WORK/rpmbuild"
    stage "$stage_dir"
    install -D -m 644 LICENSE "$stage_dir/usr/share/licenses/$NAME/LICENSE"
    mkdir -p "$top/SPECS" "$OUT"
    cat > "$top/SPECS/$NAME.spec" <<EOF
Name:           $NAME
Version:        $VERSION
Release:        $RELEASE
Summary:        $SUMMARY
License:        GPL-3.0-or-later
BuildArch:      noarch
Packager:       $MAINTAINER

Requires:       python3 >= 3.10
Requires:       python3-gobject
Requires:       gtk4 >= 4.12
Requires:       libadwaita >= 1.5
Requires:       gstreamer1
Requires:       gstreamer1-plugins-base
Requires:       gstreamer1-plugins-good
Requires:       hicolor-icon-theme
Recommends:     gstreamer1-plugin-libav
Recommends:     squeezelite

# Plain Python in /usr/share: nothing to strip, compile or scan.
%global debug_package %{nil}
%global __os_install_post %{nil}
AutoReqProv:    no

%description
Kora is a GTK4 and libadwaita client for Lyrion Music Server (formerly
Logitech Media Server). Browse the library, radio, apps and favorites,
control and sync every player on the network, and listen on this computer
through the built-in output or a managed Squeezelite.

%install
mkdir -p %{buildroot}
cp -a "$stage_dir/." %{buildroot}/

%files
%license /usr/share/licenses/$NAME/LICENSE
%doc /usr/share/doc/$NAME/README.md
/usr/bin/$NAME
/usr/share/$NAME
/usr/share/applications/$APP_ID.desktop
/usr/share/metainfo/$APP_ID.metainfo.xml
/usr/share/icons/hicolor/scalable/apps/$APP_ID.svg
/usr/share/icons/hicolor/symbolic/apps/$APP_ID-symbolic.svg
/usr/share/icons/hicolor/256x256/apps/$APP_ID.png
EOF
    rpmbuild -bb --quiet \
        --define "_topdir $top" \
        --define "_rpmdir $OUT" \
        --define "_rpmfilename %%{NAME}-%%{VERSION}-%%{RELEASE}.%%{ARCH}.rpm" \
        "$top/SPECS/$NAME.spec"
    echo "    $OUT/${NAME}-${VERSION}-${RELEASE}.noarch.rpm"
}

find kora -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

case "${1:-all}" in
    deb) build_deb ;;
    rpm) build_rpm ;;
    all)
        failed=0
        build_deb || failed=1
        build_rpm || failed=1
        [ "$failed" -eq 0 ] || { echo "One of the packages was not built (see above)." >&2; exit 1; }
        ;;
    *) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac

say "Done. Install with:"
echo "    Ubuntu:  sudo apt install ./dist/${NAME}_${VERSION}-${RELEASE}_all.deb"
echo "    Fedora:  sudo dnf install ./dist/${NAME}-${VERSION}-${RELEASE}.noarch.rpm"
