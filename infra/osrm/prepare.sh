#!/usr/bin/env bash
# Builds the OSRM routing graph for Chernivtsi from the pinned map in map.env.
#
# Idempotent: a stamp file records a hash of all inputs (source MD5, bbox, profile,
# OSRM image). If it matches, nothing is done. If any input changed, the graph is
# rebuilt; the 846 MB source is downloaded again only when the source itself changed.
#
# Everything runs in Docker (osmium and OSRM images), nothing is installed on the host.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OSRM_DIR="$ROOT_DIR/infra/osrm"
DATA_DIR="$OSRM_DIR/data"
STAMP="$DATA_DIR/chernivtsi.stamp"
EXTRACT="chernivtsi.osm.pbf"
GRAPH="chernivtsi.osrm"

# shellcheck source=infra/osrm/map.env
source "$OSRM_DIR/map.env"
SOURCE="$(basename "$OSM_SOURCE_URL")"

log() { printf '==> %s\n' "$*"; }

compose() { docker compose --project-directory "$ROOT_DIR" "$@"; }

# Tool containers run as the host user: on Linux, files written by root would be
# unreadable for the user (osrm-extract creates some with mode 0700), e.g. for the CI cache.
run_tool() { compose run --rm -T --user "$(id -u):$(id -g)" "$@"; }

# MD5 of stdin: md5sum on Linux, md5 on macOS.
md5_of() {
    if command -v md5sum >/dev/null; then
        md5sum | cut -d' ' -f1
    else
        md5 -q
    fi
}

mkdir -p "$DATA_DIR"

# The image tag is defined once in docker-compose.yml; read it from there.
OSRM_IMAGE="$(compose config --images osrm)"

INPUTS_HASH="$(
    printf 'source_md5=%s\nbbox=%s\nprofile=%s\nimage=%s\n' \
        "$OSM_SOURCE_MD5" "$OSM_BBOX" "$OSRM_PROFILE" "$OSRM_IMAGE" | md5_of
)"

if [[ -f "$STAMP" && "$(cat "$STAMP")" == "$INPUTS_HASH" ]]; then
    log "OSRM graph is up to date ($OSRM_IMAGE, bbox $OSM_BBOX), nothing to do."
    exit 0
fi

log "Inputs changed or no graph yet, building the OSRM graph."

# 1. Source map: reuse a local copy only if it is exactly the pinned snapshot.
if [[ -f "$DATA_DIR/$SOURCE" ]] && [[ "$(md5_of <"$DATA_DIR/$SOURCE")" == "$OSM_SOURCE_MD5" ]]; then
    log "Using the already downloaded $SOURCE."
else
    log "Downloading $OSM_SOURCE_URL (~850 MB)."
    rm -f "$DATA_DIR/$SOURCE"
    # Download to .part first, so an interrupted download never looks like a finished one.
    curl -fL --retry 3 -o "$DATA_DIR/$SOURCE.part" "$OSM_SOURCE_URL"
    log "Verifying MD5 of $SOURCE."
    actual_md5="$(md5_of <"$DATA_DIR/$SOURCE.part")"
    if [[ "$actual_md5" != "$OSM_SOURCE_MD5" ]]; then
        rm -f "$DATA_DIR/$SOURCE.part"
        echo "MD5 mismatch for $SOURCE: expected $OSM_SOURCE_MD5, got $actual_md5." >&2
        exit 1
    fi
    mv "$DATA_DIR/$SOURCE.part" "$DATA_DIR/$SOURCE"
fi

# osrm-routed keeps the graph files memory-mapped; replacing them under a running
# server is unreliable (on Docker Desktop the new files may fail to open).
osrm_was_running="$(compose ps -q --status running osrm)"
if [[ -n "$osrm_was_running" ]]; then
    log "Stopping the osrm service while the graph is rebuilt."
    compose stop osrm
fi

# The stamp goes away first: if any step below fails, the next run starts over.
rm -f "$STAMP" "$DATA_DIR/$GRAPH"*

# 2. Cut the city out of the whole country.
log "Extracting bbox $OSM_BBOX into $EXTRACT (osmium)."
run_tool --build osmium \
    extract -b "$OSM_BBOX" --overwrite -o "/data/$EXTRACT" "/data/$SOURCE"

# 3. Graph for the MLD algorithm.
log "osrm-extract: turning OSM ways into a road graph with the $OSRM_PROFILE profile."
run_tool osrm-prepare osrm-extract -p "/opt/$OSRM_PROFILE.lua" "/data/$EXTRACT"

log "osrm-partition: splitting the graph into nested cells."
run_tool osrm-prepare osrm-partition "/data/$GRAPH"

log "osrm-customize: precomputing travel times inside each cell."
run_tool osrm-prepare osrm-customize "/data/$GRAPH"

# 4. Written last: its presence means the graph is complete.
echo "$INPUTS_HASH" >"$STAMP"
log "OSRM graph is ready: $DATA_DIR/$GRAPH"

if [[ -n "$osrm_was_running" ]]; then
    log "Starting the osrm service with the new graph."
    compose start osrm
fi
