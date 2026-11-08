#!/usr/bin/env bash
set -euo pipefail
test "${GITHUB_ACTIONS:-}" = true
test "$(uname -s)" = Linux
WORK=$1
EVIDENCE=$2
export THEOS="$WORK/theos" JOBS=2
collect_logs() {
  for log in "$WORK/kernel/xnu-deps/"*.log; do
    test -f "$log" || continue
    cp "$log" "$EVIDENCE/dependency-$(basename "$log")"
  done
}
trap collect_logs EXIT
cd "$WORK/kernel"
bash xnu/apply.sh
python3 "$GITHUB_WORKSPACE/ci/hackios/apply_chiron.py" kernel \
  --source xnu-src/xnu-8796.121.2 --report "$EVIDENCE/chiron-kernel-overlay.json"
bash xnu-deps/setup.sh
bash xnuloader/build.sh
K=xnu-src/xnu-8796.121.2/BUILD/obj/DEVELOPMENT_ARM64_BCM2837/mach.development.bcm2837
test ! -e "$K"
START=$(date +%s)
RC=0
if [ -n "${HACKIOS_CHIRON_KERNEL_DIR:-}" ]; then
  python3 "$GITHUB_WORKSPACE/ci/hackios/chiron.py" reuse-kernel --work "$WORK" --evidence "$EVIDENCE"
  RC=$(cat "$HACKIOS_CHIRON_KERNEL_DIR/kernel-make-exit.txt")
else
  bash xnu-build.sh -j2 -k || RC=$?
  test "$(stat -c %Y "$K")" -ge "$START"
fi
test -s "$K"
test -s "$K.unstripped"
source xnu-env.sh
nm -u "$K.unstripped" > "$EVIDENCE/kernel-undefined.txt"
test ! -s "$EVIDENCE/kernel-undefined.txt"
nm -n "$K.unstripped" > "$EVIDENCE/kernel-symbols.txt"
otool -l "$K" > "$EVIDENCE/kernel-load-commands.txt"
cp "$K" "$EVIDENCE/kernel.development.hackios"
cp "$K.unstripped" "$EVIDENCE/kernel.development.hackios.unstripped"
cp xnuloader/loader.efi "$EVIDENCE/loader.efi"
printf '%s\n' "$RC" > "$EVIDENCE/kernel-make-exit.txt"
