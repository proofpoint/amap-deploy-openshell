#!/usr/bin/env bash
# Vendors OpenShell's protos, locks the interceptor's dependencies with hashes,
# and regenerates the gRPC stubs in _gen/.
#
# A dry run by default: it checks the tag, computes the import closure and
# prints what --apply would do. It never runs pip. With --apply it writes
# proto/, requirements.txt, requirements-gen.txt and _gen/, from throwaway
# virtualenvs it removes. Nothing it writes is typed by hand.
#
#   regen.sh --openshell DIR [--tag TAG] [--apply]
#
# DIR is a git checkout of OpenShell. TAG defaults to the tag in proto/SOURCE.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCKS="$HERE/locks.py"
DRY_RUN_LINE="dry run: nothing was written; add --apply to write"

# grpcio-tools 1.80.0 is the newest release with CPython 3.9 wheels.
GRPC_VERSION=1.80.0
PYTHONS="3.9 3.10 3.11 3.12 3.13"
ARCHES="x86_64 aarch64"
MANYLINUX="manylinux2014 manylinux_2_28"
PLATFORMS=""
for _arch in $ARCHES; do
    for _tag in $MANYLINUX; do PLATFORMS="$PLATFORMS ${_tag}_${_arch}"; done
done
PLATFORMS="${PLATFORMS# }"
# Versions are resolved once, on this baseline, then hashed on every target.
BASE_PY=3.9
BASE_PLAT=manylinux2014_x86_64

usage() {
    echo "usage: regen.sh --openshell DIR [--tag TAG] [--apply]" >&2
    exit 2
}

DIR=""
TAG=""
APPLY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --openshell) [ $# -ge 2 ] || usage; DIR="$2"; shift 2 ;;
        --tag) [ $# -ge 2 ] || usage; TAG="$2"; shift 2 ;;
        --apply) APPLY=1; shift ;;
        *) usage ;;
    esac
done
[ -n "$DIR" ] || usage

if [ -z "$TAG" ]; then
    TAG="$(awk '$1 == "tag" {print $2}' "$HERE/proto/SOURCE" 2>/dev/null || true)"
    [ -n "$TAG" ] || { echo "regen.sh: no --tag, and proto/SOURCE names none" >&2; exit 2; }
fi
COMMIT="$(git -C "$DIR" rev-parse "$TAG^{commit}")" \
    || { echo "regen.sh: $TAG is not a tag or commit of the OpenShell checkout" >&2; exit 1; }
CLOSURE="$(python3 "$LOCKS" closure "$DIR" "$TAG")"

if [ "$APPLY" -ne 1 ]; then
    for f in $CLOSURE; do echo "would vendor $f"; done
    echo "would lock grpcio-tools==$GRPC_VERSION (and its closure) for $PYTHONS on $PLATFORMS"
    echo "would regenerate _gen/"
    echo "$DRY_RUN_LINE"
    exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
export PIP_DISABLE_PIP_VERSION_CHECK=1

# 1. Vendor the protos.
mkdir -p "$HERE/proto"
rm -f "$HERE"/proto/*.proto
for f in $CLOSURE; do
    git -C "$DIR" show "$TAG:proto/$f" > "$HERE/proto/$f"
done
python3 "$LOCKS" source "$HERE/proto" "$TAG" "$COMMIT"

# 2. A pip of our own, so the system's is never touched.
python3 -m venv "$tmp/pip"
PIP="$tmp/pip/bin/pip"

# 3. Resolve each lock's closure on the baseline target.
download() { # dir spec...
    local dir="$1"; shift
    "$PIP" download -q --only-binary=:all: --python-version "$BASE_PY" \
        --platform "$BASE_PLAT" -d "$dir" "$@"
}
download "$tmp/rg" "grpcio-tools==$GRPC_VERSION"
python3 "$LOCKS" pins "$tmp/rg" > "$tmp/gen.pins"
PROTOBUF="$(sed -n 's/^protobuf==//p' "$tmp/gen.pins")"
[ -n "$PROTOBUF" ] || { echo "regen.sh: grpcio-tools pulled in no protobuf" >&2; exit 1; }
download "$tmp/rr" "grpcio==$GRPC_VERSION" "protobuf==$PROTOBUF"
python3 "$LOCKS" pins "$tmp/rr" > "$tmp/run.pins"

# 4. Hash every pin on every target. A project may publish a wheel under one
# manylinux tag only, so each (python, architecture) needs a wheel under at
# least one of the tags, and every wheel found is hashed.
mkdir -p "$tmp/wheels"
for py in $PYTHONS; do
    for arch in $ARCHES; do
        for pin in $(cat "$tmp/gen.pins" "$tmp/run.pins" | sort -u); do
            found=0
            for tag in $MANYLINUX; do
                if "$PIP" download -q --no-deps --only-binary=:all: \
                    --python-version "$py" --platform "${tag}_${arch}" \
                    -d "$tmp/wheels" "$pin" >/dev/null 2>&1; then
                    found=1
                fi
            done
            [ "$found" -eq 1 ] \
                || { echo "regen.sh: no wheel for $pin on python $py, $arch" >&2; exit 1; }
        done
    done
done
TARGETS=""
for py in $PYTHONS; do TARGETS="$TARGETS cp${py/./}"; done
TARGETS="${TARGETS# } on $PLATFORMS"

# 5. Write the locks. `pip hash` prints a hash per wheel file.
"$PIP" hash "$tmp"/wheels/*.whl | python3 "$LOCKS" lock \
    "$HERE/requirements-gen.txt" grpcio-tools "$tmp/gen.pins" "$TARGETS"
"$PIP" hash "$tmp"/wheels/*.whl | python3 "$LOCKS" lock \
    "$HERE/requirements.txt" grpcio,protobuf "$tmp/run.pins" "$TARGETS"

# 6. Prove the generation lock installs, then generate with what it installed.
python3 -m venv "$tmp/gen"
"$tmp/gen/bin/pip" install -q --require-hashes --only-binary=:all: \
    -r "$HERE/requirements-gen.txt"
mkdir -p "$tmp/out"
(cd "$HERE/proto" && "$tmp/gen/bin/python" -m grpc_tools.protoc -I . \
    --python_out="$tmp/out" --grpc_python_out="$tmp/out" $CLOSURE)
python3 "$LOCKS" stamp "$HERE/proto" "$tmp/out"
rm -rf "$HERE/_gen"
mkdir "$HERE/_gen"
cp "$tmp"/out/*.py "$HERE/_gen/"

# 7. Nothing written may name this host.
if grep -rF -e "$HERE" -e "$DIR" "$HERE/proto" "$HERE/_gen" \
        "$HERE"/requirements*.txt; then
    echo "regen.sh: a generated file names a host path" >&2
    exit 1
fi

for f in $CLOSURE; do echo "wrote interceptor/proto/$f"; done
echo "wrote interceptor/proto/SOURCE"
echo "wrote interceptor/requirements.txt"
echo "wrote interceptor/requirements-gen.txt"
echo "wrote interceptor/_gen/"
