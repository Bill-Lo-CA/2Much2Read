# Transaction-safe installation of user systemd unit files, shared by the three installers.
#
# Writing straight to a live unit path truncates the working copy before the replacement content
# exists, so any failure in the middle of an upgrade leaves the schedule both stopped and unusable.
# Every unit is rendered into a scratch directory first, the live files are replaced only once all
# of them are ready, and any failure after that point restores both the previous files and the
# previous timer state.
#
# Callers use, in order:
#   units_require_systemd
#   units_init      <systemd_dir> <unit file names...>
#   units_record_timer <timer>              once per timer whose state must survive a failure
#   units_render    <template> <unit> [TOKEN VALUE]...
#   units_stage_executable <path>           value for __EXECUTABLE__, quoted and checked
#   units_commit                            stop timers, replace atomically, daemon-reload
#   units_apply_timer_state <timer> <enabled|disabled> <active|inactive>

units_dir=""
units_systemd_dir=""
units_timers=""
units_files=""
units_replaced=false
units_timers_touched=false
units_newline=$(printf '\nx')
units_newline=${units_newline%x}

units_fail() {
  printf '%s\n' "$1" >&2
  exit "${2:-1}"
}

# systemctl reports "not enabled" and "could not talk to the service manager" with overlapping exit
# statuses, so an unreachable manager would otherwise be read as a disabled timer and the recorded
# state would be wrong in exactly the situation where restoring it matters.
units_require_systemd() {
  systemctl --user show --property=Version --value >/dev/null 2>&1 ||
    units_fail "cannot reach the user service manager; start it or run this from a login session"
}

units_init() {
  units_systemd_dir=$1
  shift
  units_files=$*
  mkdir -p "$units_systemd_dir" || units_fail "cannot create $units_systemd_dir"
  # The scratch directory has to share a filesystem with the live units for the final move to be
  # atomic, so it lives beside them; systemd ignores dot-directories.
  units_dir=$(mktemp -d "$units_systemd_dir/.install.XXXXXX") ||
    units_fail "cannot stage unit files in $units_systemd_dir"
  chmod 700 "$units_dir"
}

units_record_timer() {
  timer=$1
  enabled_code=0
  systemctl --user is-enabled --quiet "$timer" >/dev/null 2>&1 || enabled_code=$?
  case "$enabled_code" in
    0)
      enabled=enabled
      # "enabled" and "enabled-runtime" share exit code 0, so the status alone cannot tell a
      # permanent enablement from one meant to disappear at reboot. Restoring the second as the
      # first would quietly make it permanent, so the word decides. This is an if rather than a
      # trailing &&, which as the last command of a branch in a function returns non-zero when the
      # test fails and, under set -e, aborts the installation.
      if [ "$(systemctl --user is-enabled "$timer" 2>/dev/null)" = "enabled-runtime" ]; then
        enabled="enabled-runtime"
      fi
      ;;
    1 | 4) enabled=disabled ;;
    *) units_fail "cannot determine whether $timer is enabled" ;;
  esac
  active_code=0
  systemctl --user is-active --quiet "$timer" >/dev/null 2>&1 || active_code=$?
  case "$active_code" in
    0) active=active ;;
    3 | 4) active=inactive ;;
    *) units_fail "cannot determine whether $timer is active" ;;
  esac
  units_timers="$units_timers $timer"
  printf '%s %s\n' "$enabled" "$active" > "$units_dir/state.$timer"
}

units_require_inactive_service() {
  state=$(systemctl --user show --property=ActiveState --value "$1") ||
    units_fail "cannot determine whether $1 is active"
  case "$state" in
    inactive | failed) ;;
    *) units_fail "stop $1 before installing" ;;
  esac
}

# A timer enabled with --runtime is enabled now and gone at reboot. It counts as enabled wherever
# the question is "is a schedule running", and only its restoration has to keep the two apart.
units_enabled_like() {
  case "$1" in
    enabled | enabled-runtime) return 0 ;;
  esac
  return 1
}

units_disable_both() {
  units_disable_failed=0
  systemctl --user disable "$1" || units_disable_failed=1
  systemctl --user disable --runtime "$1" || units_disable_failed=1
  return "$units_disable_failed"
}

# Enabled and active are independent: a timer can be enabled but stopped for maintenance, or
# started for this session without being enabled. Restoring only the enable bit would silently
# start a timer the operator had stopped, or leave a running one stopped for good.
#
# Every pair is stated, including the two that used to be a no-op. Moving a timer only upwards was
# sound while the sole caller restored timers that units_stop_timers had already put at the bottom;
# a timer enabled after the commit, by this same function applying an operator's answer, sits above
# its recorded state instead, and asking for "disabled inactive" has to bring it down rather than
# quietly report success. An unrecognised pair fails for the same reason.
#
# "enable --now" is kept for the one pair that means it, and "disable --now" deliberately is not:
# that stays the signature of units_stop_timers having interrupted a running schedule.
units_apply_timer_state() {
  units_timers_touched=true
  units_apply_failed=0
  case "$2 $3" in
    "enabled active") systemctl --user enable --now "$1" || units_apply_failed=1 ;;
    "enabled inactive")
      systemctl --user enable "$1" || units_apply_failed=1
      systemctl --user stop "$1" || units_apply_failed=1
      ;;
    "enabled-runtime active")
      systemctl --user enable --runtime "$1" || units_apply_failed=1
      systemctl --user start "$1" || units_apply_failed=1
      ;;
    "enabled-runtime inactive")
      systemctl --user enable --runtime "$1" || units_apply_failed=1
      systemctl --user stop "$1" || units_apply_failed=1
      ;;
    # Both scopes are cleared. "disable" removes symlinks from the unit configuration directory,
    # which --runtime selects, so a plain disable need not reach a runtime enablement. Settling
    # that either way would mean enabling a unit in the live user manager, so instead both are
    # cleared; whichever was not there is a no-op.
    "disabled active")
      units_disable_both "$1" || units_apply_failed=1
      systemctl --user start "$1" || units_apply_failed=1
      ;;
    "disabled inactive")
      units_disable_both "$1" || units_apply_failed=1
      systemctl --user stop "$1" || units_apply_failed=1
      ;;
    *) return 1 ;;
  esac
  return "$units_apply_failed"
}

# Reports whether every timer came back. Each step swallows its own failure so one timer that
# cannot be restored does not abort the loop and leave the rest untouched, but the failure is
# carried out rather than discarded: a rollback that cannot say whether it worked is one the
# caller has to assume worked.
units_restore_timers() {
  units_timers_restored=true
  units_timers_lost=""
  for timer in $units_timers; do
    [ -f "$units_dir/state.$timer" ] || continue
    # Both words or nothing. "read" only fails on an empty file, so a line holding one word would
    # otherwise succeed with an empty "active", fall through units_apply_timer_state's case to the
    # no-op, and be counted as a timer restored - the exact false report this rollback exists to end.
    if ! read -r enabled active < "$units_dir/state.$timer" || [ -z "$enabled" ] || [ -z "$active" ]; then
      units_timers_restored=false
      units_timers_lost="$units_timers_lost $timer"
      continue
    fi
    if ! units_apply_timer_state "$timer" "$enabled" "$active" >/dev/null 2>&1; then
      units_timers_restored=false
      units_timers_lost="$units_timers_lost $timer"
    fi
  done
  [ "$units_timers_restored" = true ]
}

units_stop_timers() {
  for timer in $units_timers; do
    read -r enabled active < "$units_dir/state.$timer" || continue
    units_enabled_like "$enabled" || [ "$active" = active ] || continue
    units_timers_touched=true
    systemctl --user disable --now "$timer" || units_fail "failed to stop and disable $timer"
    # The runtime scope too, for the same reason units_apply_timer_state clears both.
    [ "$enabled" = enabled-runtime ] || continue
    systemctl --user disable --runtime "$timer" || units_fail "failed to disable $timer"
  done
}

# sed would be the obvious tool and is the wrong one: an "&" in the replacement expands to the
# whole match and a "|" ends the expression, so a repository path containing either silently
# writes the wrong ExecStart or fails after the destination has already been truncated.
units_replace_token() {
  file=$1
  token=$2
  value=$3
  while IFS= read -r line || [ -n "$line" ]; do
    rest=$line
    out=""
    while :; do
      case "$rest" in
        *"$token"*)
          out="$out${rest%%"$token"*}$value"
          rest=${rest#*"$token"}
          ;;
        *)
          out="$out$rest"
          break
          ;;
      esac
    done
    printf '%s\n' "$out"
  done < "$file" > "$file.next"
  mv "$file.next" "$file"
}

units_render() {
  template=$1
  unit=$2
  shift 2
  [ -f "$template" ] || units_fail "unit template not found: $template"
  cp "$template" "$units_dir/$unit" || units_fail "cannot stage $unit"
  while [ "$#" -gt 1 ]; do
    units_replace_token "$units_dir/$unit" "$1" "$2"
    shift 2
  done
  chmod 644 "$units_dir/$unit"
}

# systemd splits a command line on whitespace, so an unquoted path containing a space becomes a
# different executable and a stray argument. Quoting fixes that; a quote or newline inside the
# path itself has no safe rendering, so it is refused instead.
#
# Quoting does not stop specifier expansion: "%h" inside ExecStart becomes the home directory
# whether or not it is in quotes, so a checkout under a directory named with a percent would run
# something else entirely. "%%" is systemd's literal percent, and escaping is done here rather than
# by refusing the path, because a percent in a directory name is legal and not the operator's fault.
units_stage_executable() {
  case "$1" in
    *'"'* | *\\* | *"$units_newline"*)
      units_fail "executable path must not contain quotes, backslashes, or newlines: $1"
      ;;
  esac
  units_escaped=""
  units_rest=$1
  while :; do
    case "$units_rest" in
      *%*)
        units_escaped="$units_escaped${units_rest%%%*}%%"
        units_rest=${units_rest#*%}
        ;;
      *)
        units_escaped="$units_escaped$units_rest"
        break
        ;;
    esac
  done
  printf '"%s"' "$units_escaped"
}

# Timers are checked too. A schedule value that reaches the template malformed produces a unit
# systemd refuses to load, and finding that out here means the live files have not been touched.
units_verify() {
  command -v systemd-analyze >/dev/null 2>&1 || return 0
  for unit in $units_files; do
    systemd-analyze --user verify "$units_dir/$unit" >/dev/null 2>&1 ||
      units_fail "the rendered $unit is not a valid unit file"
  done
}

# Reports whether every unit came back. The backup is copied to a scratch name and moved into
# place rather than copied over the live path: units_dir sits inside units_systemd_dir, so the move
# is atomic, and a restore interrupted half way cannot leave a truncated unit behind - which is the
# very state the rollback exists to undo.
units_rollback() {
  units_units_restored=true
  units_units_lost=""
  units_reload_failed=false
  for unit in $units_files; do
    if [ -f "$units_dir/backup.$unit" ]; then
      if cp "$units_dir/backup.$unit" "$units_dir/restore.$unit" 2>/dev/null &&
        mv "$units_dir/restore.$unit" "$units_systemd_dir/$unit" 2>/dev/null; then
        continue
      fi
    elif rm -f "$units_systemd_dir/$unit" 2>/dev/null; then
      continue
    fi
    units_units_restored=false
    units_units_lost="$units_units_lost $unit"
  done
  # A rollback is not finished when the files are back: systemd is still running the definitions it
  # loaded from the units that have just been replaced. Files on disk and service-manager state
  # disagreeing is a worse place to be than either failure alone, so the reload counts.
  if ! systemctl --user daemon-reload >/dev/null 2>&1; then
    units_units_restored=false
    units_reload_failed=true
  fi
  [ "$units_units_restored" = true ]
}

# Stops units before an uninstaller removes them, telling "there is nothing installed to stop" apart
# from "systemd would not stop it". The installed file is the test, because these are paths we own;
# any failure from systemctl itself is then a real service-manager problem rather than the ordinary
# case of uninstalling something that was never there. Callers set units_systemd_dir first and must
# not remove anything unless this returns success: deleting the unit file of a timer still running
# leaves the files and the service manager disagreeing, which is the state a rollback exists to
# prevent.
units_teardown() {
  units_teardown_failed=false
  for unit in "$@"; do
    [ -e "$units_systemd_dir/$unit" ] || continue
    case "$unit" in
      *.timer)
        units_apply_timer_state "$unit" disabled inactive || {
          printf '%s\n' "failed to stop and disable $unit" >&2
          units_teardown_failed=true
        }
        ;;
      *)
        systemctl --user stop "$unit" || {
          printf '%s\n' "failed to stop $unit" >&2
          units_teardown_failed=true
        }
        ;;
    esac
  done
  [ "$units_teardown_failed" = false ]
}

units_commit() {
  for unit in $units_files; do
    [ -f "$units_dir/$unit" ] || units_fail "unit was never rendered: $unit"
    if [ -e "$units_systemd_dir/$unit" ]; then
      cp "$units_systemd_dir/$unit" "$units_dir/backup.$unit" || units_fail "cannot back up $unit"
    fi
  done
  units_verify
  units_stop_timers
  units_replaced=true
  for unit in $units_files; do
    mv "$units_dir/$unit" "$units_systemd_dir/$unit" || units_fail "cannot install $unit"
  done
  systemctl --user daemon-reload || units_fail "systemctl daemon-reload failed"
}

# A signal handler must be told the status to exit with. POSIX leaves "$?" inside a trap as the
# status of the last completed command, which after a successful step is 0 - so a handler that
# reads "$?" would decide the run succeeded, skip the restore, and exit 0 with the timer stopped.
units_cleanup() {
  units_status=$1
  trap - EXIT INT TERM HUP
  units_incomplete=false
  if [ "$units_status" -ne 0 ]; then
    # The timers come back first. A first installation has no backups, so the rollback below deletes
    # the unit files outright, and a timer cannot be disabled once the file naming it is gone - so
    # the state is put right while those files are still on disk. The rollback's own daemon-reload
    # then follows, as it always did.
    #
    # The guard is "was any timer touched", not "did the commit stop one": applying the operator's
    # answer changes timers too, and after a failure part way through that the commit had nothing
    # to stop, so a guard asking about the commit skips the restore exactly when it is needed.
    if [ "$units_timers_touched" = true ]; then
      if units_restore_timers; then
        printf '%s\n' "installation failed; the previous timer state was restored" >&2
      else
        units_incomplete=true
        printf '%s\n' "installation failed and these timers could NOT be restored:$units_timers_lost" >&2
      fi
    fi
    if [ "$units_replaced" = true ]; then
      if units_rollback; then
        printf '%s\n' "installation failed; the previous unit files were restored" >&2
      else
        units_incomplete=true
        if [ -n "$units_units_lost" ]; then
          printf '%s\n' "installation failed and these unit files could NOT be restored:$units_units_lost" >&2
        fi
        if [ "$units_reload_failed" = true ]; then
          printf '%s\n' \
            "installation failed; the previous unit files are back on disk but systemd did not reload them," \
            "so it may still be running the replaced definitions - recover with: systemctl --user daemon-reload" >&2
        fi
      fi
    fi
  fi
  # Deleting the staging directory after a rollback that did not finish would take the backups with
  # it, and after a failed restore those are the only remaining copies of the working units.
  if [ "$units_incomplete" = true ]; then
    printf '%s\n' "the previous unit files are kept for recovery in: $units_dir" >&2
  elif [ -n "$units_dir" ]; then
    rm -rf "$units_dir"
  fi
  exit "$units_status"
}

units_trap() {
  trap 'units_cleanup $?' EXIT
  trap 'units_cleanup 130' INT
  trap 'units_cleanup 143' TERM
  trap 'units_cleanup 129' HUP
}
