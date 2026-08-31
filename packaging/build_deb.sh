#!/usr/bin/env bash
# Build the ezkaraoke .deb package (Debian/Ubuntu).
#
# Usage:  packaging/build_deb.sh
# Output: dist/ezkaraoke_<version>_<arch>.deb
#
# Requirements: dpkg-deb (ships with Debian/Ubuntu).
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

VERSION="$(grep -oP '__version__ = "\K[^"]+' ezkaraoke/__init__.py)"
ARCH="$(dpkg --print-architecture)"
PKG="ezkaraoke_${VERSION}_${ARCH}"

STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT
mkdir -p \
  "$STAGING/DEBIAN" \
  "$STAGING/usr/bin" \
  "$STAGING/usr/lib/ezkaraoke" \
  "$STAGING/usr/share/applications" \
  "$STAGING/usr/share/icons/hicolor/512x512/apps" \
  "$STAGING/usr/share/icons/hicolor/256x256/apps" \
  "$STAGING/usr/share/icons/hicolor/128x128/apps" \
  "$STAGING/usr/share/icons/hicolor/64x64/apps"

# --- application code -------------------------------------------------------
cp -r ezkaraoke "$STAGING/usr/lib/ezkaraoke/ezkaraoke"
find "$STAGING/usr/lib/ezkaraoke" -name "__pycache__" -type d -exec rm -rf {} +
find "$STAGING/usr/lib/ezkaraoke" -name "*.pyc" -delete

# --- launcher ---------------------------------------------------------------
cat > "$STAGING/usr/bin/ezkaraoke" <<'EOF'
#!/usr/bin/env python3
import sys

sys.path.insert(0, "/usr/lib/ezkaraoke")

from ezkaraoke.main import main

sys.exit(main())
EOF
chmod 755 "$STAGING/usr/bin/ezkaraoke"

# --- python-vlc (not packaged by Ubuntu): vendored single-file module -------
# Keep packaging/vendor/vlc.py in sync with requirements.txt.
cp packaging/vendor/vlc.py "$STAGING/usr/lib/ezkaraoke/vlc.py"
cp packaging/vendor/LICENSE.python-vlc "$STAGING/usr/share/doc/ezkaraoke/LICENSE.python-vlc" 2>/dev/null \
  || { mkdir -p "$STAGING/usr/share/doc/ezkaraoke"
       cp packaging/vendor/LICENSE.python-vlc "$STAGING/usr/share/doc/ezkaraoke/LICENSE.python-vlc"; }
cp LICENSE "$STAGING/usr/share/doc/ezkaraoke/copyright" 2>/dev/null || true

# --- desktop entry + icons --------------------------------------------------
cp packaging/ezkaraoke.desktop "$STAGING/usr/share/applications/ezkaraoke.desktop"
for size in 512 256 128 64; do
  cp "packaging/icons/ezkaraoke_${size}.png" \
     "$STAGING/usr/share/icons/hicolor/${size}x${size}/apps/ezkaraoke.png"
done

# --- control ----------------------------------------------------------------
cat > "$STAGING/DEBIAN/control" <<EOF
Package: ezkaraoke
Version: ${VERSION}
Architecture: ${ARCH}
Maintainer: kknddandy <kknddandy@users.noreply.github.com>
Installed-Size: $(du -sk --exclude=DEBIAN "$STAGING" | cut -f1)
Depends: python3 (>= 3.10), python3-pyside6.qtwidgets, python3-pypinyin, libvlc5
Recommends: vlc
Section: sound
Priority: optional
Description: Local KTV karaoke player
 EzKaraoke is a local KTV (karaoke) player for home use. It scans a
 music folder of video files named "Artist-Title-xxx" and provides
 singer-based or letter-based song selection with auto-fetched artist
 avatars. Videos play through VLC with original/instrumental audio
 track switching and pitch shift (semitone) controls.
EOF
chmod 644 "$STAGING/DEBIAN/control"

# --- build ------------------------------------------------------------------
mkdir -p dist
dpkg-deb --build --root-owner-group "$STAGING" "dist/${PKG}.deb"
echo "Built: dist/${PKG}.deb"
