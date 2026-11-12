#!/usr/bin/env bash
# Precompile <bits/stdc++.h> with the judge's exact C++ flags.
# Most contestants include it, and parsing it costs about a second per
# submission; with the .gch it is a quarter of that. GCC only uses the .gch
# when the flags match, so keep these in sync with languages.py.
set -euo pipefail
OUT=/usr/local/lib/judgebox-pch/bits
HEADER=$(echo '#include <bits/stdc++.h>' | g++ -std=gnu++17 -H -x c++ -fsyntax-only - 2>&1 >/dev/null | awk '/bits\/stdc\+\+\.h$/ && !f {print $2; f=1}')
mkdir -p "$OUT"
g++ -O2 -std=gnu++17 -x c++-header "$HEADER" -o "$OUT/stdc++.h.gch"
echo "built $OUT/stdc++.h.gch from $HEADER"
