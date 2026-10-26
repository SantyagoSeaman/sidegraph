"""The public 0.17.2 directory-lock helper, frozen for upgrade regression tests."""

LEGACY_HELPER = r"""#!/bin/sh
# Installed by sidegraph-init: keeps graphify-out/graph.json current for Sidegraph.
# Main checkout only: a linked worktree reads the main checkout's graph. The rebuild runs
# in the background so git never waits. A "want" flag plus a lock directory run one rebuild
# at a time and never miss a request that arrives during one.
hook=$1; shift
[ "$(git rev-parse --path-format=absolute --git-dir 2>/dev/null)" = \
  "$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" ] || exit 0
case $hook in post-checkout) [ "$3" = 1 ] && [ "$1" != "$2" ] || exit 0 ;; esac
top=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
graphify=$(command -v graphify 2>/dev/null) || graphify=@GRAPHIFY@
[ -n "$graphify" ] && [ -x "$graphify" ] || exit 0
lock=$common/sidegraph-graph-refresh.lock
want=$common/sidegraph-graph-refresh.want
log=$common/sidegraph-graph-refresh.log
: > "$want" 2>/dev/null || exit 0
(
  me=$(exec sh -c 'echo $PPID')
  take() { mkdir "$lock" 2>/dev/null && echo "$me" > "$lock/pid"; }
  if ! take; then
    pid=$(cat "$lock/pid" 2>/dev/null)
    if [ -n "$pid" ]; then
      kill -0 "$pid" 2>/dev/null && exit 0      # a runner is alive; it will see $want
    else                                        # the owner is mid-write, or died there
      [ -n "$(find "$lock" -maxdepth 0 -mmin +10 2>/dev/null)" ] || exit 0
    fi
    mkdir "$lock.gate" 2>/dev/null || exit 0    # one stale-breaker at a time
    [ "$(cat "$lock/pid" 2>/dev/null)" = "$pid" ] &&
      { [ -n "$pid" ] || [ -n "$(find "$lock" -maxdepth 0 -mmin +10 2>/dev/null)" ]; } &&
      rm -rf "$lock"
    rmdir "$lock.gate"; take || exit 0
  fi
  cd "$top" || { rm -rf "$lock"; exit 0; }
  while :; do
    while [ -e "$want" ]; do
      rm -f "$want"
      GRAPHIFY_NO_AUTO_REFRESH=1 PYTHONHASHSEED=0 "$graphify" update . > "$log" 2>&1
    done
    rm -rf "$lock"; [ -e "$want" ] || break; take || break
  done
) </dev/null >/dev/null 2>&1 &
exit 0
"""
