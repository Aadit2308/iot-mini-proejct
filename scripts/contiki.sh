#!/usr/bin/env bash
# Contiki-NG / Cooja helper, run inside WSL Ubuntu (launch.bat calls it for you).
#
#   bash scripts/contiki.sh setup              # one-time: gcc, make, JDK, clone Contiki-NG+Cooja (sudo)
#   bash scripts/contiki.sh check              # is everything installed?
#   bash scripts/contiki.sh build-native [N]   # build the node for the native target, scenario N
#   bash scripts/contiki.sh run-native  [N]    # build + run; MOISTURE lines go to stdout
#   bash scripts/contiki.sh cooja       [N]    # open Cooja with the irrigation simulation loaded
#
# N = FAULT_SCENARIO: 0 normal, 1 invalid, 2 negative, 3 missing, 4 delayed,
#     5 intermittent, 6 malformed, 7 mixed (default)
#
# CONTIKI defaults to ~/contiki-ng (override with CONTIKI=/path).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODE_DIR="$PROJECT_DIR/simulator/contiki"
export CONTIKI="${CONTIKI:-$HOME/contiki-ng}"
COOJA_DIR="$CONTIKI/tools/cooja"
CSC_TEMPLATE="$NODE_DIR/irrigation.csc.in"

cmd="${1:-help}"
SCENARIO="${2:-7}"

log()  { printf '\033[1;32m==> %s\033[0m\n' "$*" >&2; }
fail() { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

check_scenario() {
    [[ "$SCENARIO" =~ ^[0-7]$ ]] || fail "scenario must be 0-7 (got '$SCENARIO')"
}

check() {
    local ok=0
    for tool in gcc make git; do
        if command -v "$tool" >/dev/null; then echo "  [ok]      $tool"; else echo "  [missing] $tool"; ok=1; fi
    done
    if command -v java >/dev/null; then echo "  [ok]      java ($(java -version 2>&1 | head -1))"
    else echo "  [missing] java (needed for Cooja only)"; ok=1; fi
    if [[ -f "$CONTIKI/Makefile.include" ]]; then echo "  [ok]      Contiki-NG at $CONTIKI"
    else echo "  [missing] Contiki-NG at $CONTIKI"; ok=1; fi
    if [[ -x "$COOJA_DIR/gradlew" ]]; then echo "  [ok]      Cooja at $COOJA_DIR"
    else echo "  [missing] Cooja at $COOJA_DIR"; ok=1; fi
    return $ok
}

setup() {
    log "Installing compiler, make and JDK (sudo password required)"
    sudo apt-get update
    sudo apt-get install -y build-essential git
    sudo apt-get install -y openjdk-25-jdk || sudo apt-get install -y default-jdk

    if [[ -f "$CONTIKI/Makefile.include" ]]; then
        log "Contiki-NG already at $CONTIKI"
        git -C "$CONTIKI" submodule update --init --depth 1 tools/cooja || true
    else
        log "Cloning Contiki-NG (with Cooja) into $CONTIKI"
        git clone --recursive --depth 1 --shallow-submodules \
            https://github.com/contiki-ng/contiki-ng.git "$CONTIKI"
    fi

    log "Pre-building Cooja (first run downloads Gradle, ~2-5 min)"
    (cd "$COOJA_DIR" && ./gradlew --quiet jar) || log "Cooja pre-build failed; it will be retried on first launch"

    log "Test build of the sensor node (native target)"
    build_native
    log "Setup complete"
    check
}

build_native() {
    check_scenario
    [[ -f "$CONTIKI/Makefile.include" ]] || fail "Contiki-NG not found at $CONTIKI - run: bash scripts/contiki.sh setup"
    command -v gcc >/dev/null || fail "gcc missing - run: bash scripts/contiki.sh setup"
    cd "$NODE_DIR"
    # A different scenario needs a rebuild: the value is baked in at compile time.
    local stamp="build/native/.scenario"
    if [[ ! -f "$stamp" || "$(cat "$stamp")" != "$SCENARIO" ]]; then
        rm -rf build/native
    fi
    log "Building sensor_node.native (FAULT_SCENARIO=$SCENARIO)"
    make -j"$(nproc)" TARGET=native FAULT_SCENARIO="$SCENARIO" sensor_node >&2
    echo "$SCENARIO" > "$stamp"
}

run_native() {
    build_native
    log "Running sensor node (Ctrl+C to stop); readings every 5 s"
    cd "$NODE_DIR"
    # Line-buffer stdout: otherwise printf output sits in a 4 KB pipe buffer.
    exec stdbuf -oL ./build/native/sensor_node.native
}

cooja() {
    check_scenario
    [[ -x "$COOJA_DIR/gradlew" ]] || fail "Cooja not found at $COOJA_DIR - run: bash scripts/contiki.sh setup"
    command -v java >/dev/null || fail "java missing - run: bash scripts/contiki.sh setup"

    # Generate the simulation file for the chosen scenario (the mote type's
    # build command does a clean build with FAULT_SCENARIO=N).
    local csc="$NODE_DIR/irrigation.csc"
    # Cooja runs `make` without HOME/CONTIKI set, so the absolute Contiki-NG path
    # is written into the mote build commands (CONTIKI=@CONTIKI@).
    sed -e "s|@SCENARIO@|$SCENARIO|g" -e "s|@CONTIKI@|$CONTIKI|g" "$CSC_TEMPLATE" > "$csc"

    log "Starting Cooja with $csc (FAULT_SCENARIO=$SCENARIO)"
    log "Cooja compiles the node, then runs at real-time speed; Serial Socket on port 60001."
    cd "$COOJA_DIR"
    exec ./gradlew --quiet run --args="--gui --autostart --contiki=$CONTIKI $csc"
}

case "$cmd" in
    setup)        setup ;;
    check)        check ;;
    build-native) build_native ;;
    run-native)   run_native ;;
    cooja)        cooja ;;
    *)            sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
esac
