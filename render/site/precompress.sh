#!/usr/bin/env bash
# precompress.sh · brotli-precompress the text site assets so the control plane can
# serve .br when the client accepts it. glb/png/woff2/ktx2 are entropy-dense already ·
# only js is worth it (measured: three.module.js ~1.27MB to ~330KB brotli).
set -euo pipefail
cd "$(dirname "$0")/../.."
command -v brotli >/dev/null || { echo "brotli not found"; exit 1; }
find web/assets/site -type f -name '*.js' | while read -r f; do
  brotli -f -q 11 -o "$f.br" "$f"
  printf '%-52s %8d -> %8d br\n' "${f#web/assets/site/}" "$(wc -c <"$f")" "$(wc -c <"$f.br")"
done
