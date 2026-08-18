#!/usr/bin/env bash
# Builds a Chrome Web Store-ready ZIP from this folder.
#
#   ./package.sh                                   # uses manifest as-is
#   ./package.sh https://api.your-college.edu      # rewrites the API origin
#
# The Web Store wants a ZIP whose ROOT contains manifest.json — not a ZIP
# containing a folder that contains it. Getting that wrong is the most
# common first rejection, so this script zips from inside the directory
# rather than zipping the directory.
set -euo pipefail

cd "$(dirname "$0")"

API_ORIGIN="${1:-}"

# Builds land in build/, NOT the repository root.
#
# A ZIP sitting next to the `extension/` folder reads as a second copy of
# the extension — it is not obvious at a glance which one Chrome should be
# pointed at, and the answer ("the folder, always, unless you are
# submitting to the Web Store") is not something a directory listing can
# say. `extension/` is the single source of truth; this is a build
# artefact, so it goes where build artefacts go.
OUT="${AUTONOMIZE_ZIP_OUT:-../build/autonomize-extension.zip}"
mkdir -p "$(dirname "$OUT")"

if [ -n "$API_ORIGIN" ]; then
  # Chrome blocks fetch to any origin not in host_permissions, and the
  # failure is silent from the page's side — the retry queue fills and
  # nothing uploads. So the origin is rewritten in BOTH places it must
  # match: the manifest permission and the default backendUrl.
  python3 - "$API_ORIGIN" <<'PY'
import collections, json, pathlib, re, sys

origin = sys.argv[1].rstrip("/")
if not origin.startswith("https://"):
    sys.exit(f"Refusing: {origin!r} is not https. Chrome requires TLS for a "
             "packaged extension's host permission.")

manifest = pathlib.Path("manifest.json")
data = json.loads(manifest.read_text(), object_pairs_hook=collections.OrderedDict)
data["host_permissions"] = [
    h for h in data["host_permissions"]
    if "localhost" in h or "127.0.0.1" in h
] + [origin + "/*"]
manifest.write_text(json.dumps(data, indent=2) + "\n")

background = pathlib.Path("background.js")
source = background.read_text()
patched, count = re.subn(r'backendUrl:\s*"[^"]*"', f'backendUrl: "{origin}"', source, count=1)
if count != 1:
    sys.exit("Could not find DEFAULT_SETTINGS.backendUrl in background.js")
background.write_text(patched)

print(f"  host_permissions -> {data['host_permissions']}")
print(f"  backendUrl       -> {origin}")
PY
fi

# Everything the manifest actually references, and nothing else. A ZIP
# containing the packaging script, notes or a .DS_Store is not rejected,
# but it ships files nobody reviewed.
rm -f "$OUT"
zip -q -r "$OUT" \
  manifest.json \
  background.js content-script.js site-map.js telemetry.js \
  popup.html popup.css popup.js \
  icons \
  -x "*.DS_Store" "*/.*"

echo
echo "wrote $(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"
unzip -l "$OUT" | tail -n +4 | head -20
echo
echo "manifest.json is at the ZIP root:"
# Written to a variable first rather than piped into `grep -q`. Under
# `set -o pipefail`, grep -q closes the pipe as soon as it matches, unzip
# takes SIGPIPE, and the pipeline reports failure ON SUCCESS — so the
# check printed "the store will reject this" for a perfectly valid ZIP.
listing="$(unzip -l "$OUT")"
if printf '%s\n' "$listing" | grep -qE "[[:space:]]manifest\.json$"; then
  echo "  yes"
else
  echo "  NO — the store will reject this"
  exit 1
fi
