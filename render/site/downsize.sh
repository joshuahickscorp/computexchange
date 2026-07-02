#!/usr/bin/env bash
# downsize.sh · post-render pipeline: 8-bit webification + srcset downsizes, the
# og:image composite, and the continuity contact sheet (gate 6). Repo root only.
set -euo pipefail
cd "$(dirname "$0")/../.."
python3 render/site/webify.py
python3 render/site/og.py
python3 render/site/continuity.py
