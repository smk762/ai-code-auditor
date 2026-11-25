#!/usr/bin/env bash
# wrap.sh <conda prefix> <out dir>: a wrapper for each geo command, which names the env's data dirs itself.
# validate_patch's children get an allowlisted environment (auditor/proc.py), so ENV lines would not reach them.
# PROJ_NETWORK=OFF: a test never downloads a grid.
set -euo pipefail
prefix=$1 out=$2
mkdir -p "$out"
for exe in "$prefix"/bin/{gdal,ogr,pdal,untwine,proj,cs2cs,cct,gie,geod}*; do
    [ -x "$exe" ] && [ -f "$exe" ] || continue
    name=$(basename "$exe")
    case $name in *-config) continue ;; esac
    cat > "$out/$name" <<EOF
#!/bin/sh
export PROJ_DATA="\${PROJ_DATA:-$prefix/share/proj}" GDAL_DATA="\${GDAL_DATA:-$prefix/share/gdal}" PROJ_NETWORK="\${PROJ_NETWORK:-OFF}"
exec $exe "\$@"
EOF
    chmod 755 "$out/$name"
done
