#!/usr/bin/env bash
# Context Bridge — one-command deployment for a single agent host.
#
# Public-safe by design: this script contains NO host-specific values. The hub path,
# port, bot state directories, chat ids and interpreter paths all come from an
# untracked env file next to the repository:
#
#   cp deploy.env.example deploy.env   # then edit it for this host
#   ./scripts/deploy.sh                # start (default)
#
# The env file is resolved in this order:
#   $CTXBRIDGE_DEPLOY_ENV, ./deploy.env, <repo>/deploy.env
#
# It is sourced as shell, so values may reference earlier variables:
#   HUB_DIR=/srv/context-hub
#   CONFIG=$HUB_DIR/config.toml
#
# Services are supervised by tmux sessions (`ctxbridge-sync`, `ctxbridge-serve`):
# this class of host has no systemd user services. Logs live under <HUB_DIR>/run/.
#
# Usage:
#   deploy.sh [start] [--dry-run] [--force-config]
#   deploy.sh config [--force-config] [--print]   # write config.toml only, no services
#   deploy.sh status | stop | restart | logs [--lines N]
#
# Exit codes: 0 ok, 1 deployment/health failure, 2 usage error.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)

ACTION="start"
DRY_RUN=0
FORCE_CONFIG=0
PRINT_CONFIG=0
LOG_LINES=60

usage() {
  sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

die() {
  printf '[deploy] ERROR: %s\n' "$*" >&2
  exit 1
}
log() { printf '[deploy] %s\n' "$*"; }
warn() { printf '[deploy] WARN: %s\n' "$*" >&2; }
is_dry() { [ "$DRY_RUN" = 1 ]; }
# run: execute a command, or print it when --dry-run is set.
run() {
  if is_dry; then
    local rendered
    rendered=$(printf '%q ' "$@")
    printf '[dry-run] %s\n' "${rendered% }"
    return 0
  fi
  "$@"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    start | stop | restart | status | logs | config) ACTION=$1 ;;
    --dry-run | -n) DRY_RUN=1 ;;
    --force-config) FORCE_CONFIG=1 ;;
    --print) PRINT_CONFIG=1 ;;
    --lines) shift; LOG_LINES=${1:?--lines needs a value} ;;
    -h | --help) usage; exit 0 ;;
    *) printf '[deploy] unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# --------------------------------------------------------------------------------------
# deployment environment
# --------------------------------------------------------------------------------------
DEPLOY_ENV="${CTXBRIDGE_DEPLOY_ENV:-}"
if [ -z "$DEPLOY_ENV" ]; then
  for candidate in "$PWD/deploy.env" "$REPO_ROOT/deploy.env"; do
    if [ -f "$candidate" ]; then DEPLOY_ENV=$candidate; break; fi
  done
fi

NEEDS_ENV=0
case "$ACTION" in start | restart) NEEDS_ENV=1 ;; esac

if [ -n "$DEPLOY_ENV" ] && [ -f "$DEPLOY_ENV" ]; then
  # shellcheck source=/dev/null
  set -a
  # shellcheck disable=SC1090
  . "$DEPLOY_ENV"
  set +a
  log "loaded deployment env: $DEPLOY_ENV"
elif [ "$NEEDS_ENV" = 1 ]; then
  cat >&2 <<'EOF'
[deploy] ERROR: no deployment env file found.

  cp deploy.env.example deploy.env
  # edit deploy.env: HUB_DIR, CONFIG, PORT, BOTS, BOT_<ID>_STATE_DIR, CTXBRIDGE_PY, ...
  ./scripts/deploy.sh

deploy.env is untracked and must never be committed: it holds this host's paths and ids.
EOF
  exit 1
else
  warn "no deployment env file found; using defaults (mostly useful for status/stop)"
fi

abspath() {
  case "$1" in
    /*) printf '%s' "$1" ;;
    *) printf '%s/%s' "$PWD" "$1" ;;
  esac
}

HUB_DIR="${HUB_DIR:-$REPO_ROOT/hub}"
HUB_DIR=$(abspath "$HUB_DIR")
CONFIG="${CONFIG:-$HUB_DIR/config.toml}"
CONFIG=$(abspath "$CONFIG")
LOG_DIR="${LOG_DIR:-$HUB_DIR/run}"
LOG_DIR=$(abspath "$LOG_DIR")
PORT="${PORT:-8080}"
BIND_HOST="${BIND_HOST:-0.0.0.0}"
HEALTH_URL="${HEALTH_URL:-http://localhost:$PORT/api/health}"
PUBLIC_URL="${PUBLIC_URL:-}"
PUBLIC_URL="${PUBLIC_URL%/}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-30}"
SYNC_INTERVAL="${SYNC_INTERVAL:-60}"
SESSIONS_ROOT="${SESSIONS_ROOT:-}"
MASK_INTERNAL_DOMAINS="${MASK_INTERNAL_DOMAINS:-}"
CTXBRIDGE_PY="${CTXBRIDGE_PY:-python3}"
CTXBRIDGE_BIN="${CTXBRIDGE_BIN:-}"
CTXBRIDGE_INSTALL="${CTXBRIDGE_INSTALL:-auto}"
PATH_PREFIX="${PATH_PREFIX:-}"
SERVICE_ENV="${SERVICE_ENV:-}"
SKILLS_DIR="${SKILLS_DIR:-$HOME/.agents/skills/context-bridge}"
SKILL_MODE="${SKILL_MODE:-symlink}"
SKILL_SRC="${SKILL_SRC:-$REPO_ROOT/integrations/dsh-skill}"
SYNC_SESSION="${SYNC_SESSION:-ctxbridge-sync}"
SERVE_SESSION="${SERVE_SESSION:-ctxbridge-serve}"
BOTS="${BOTS:-}"
FEISHU_ENABLED="${FEISHU_ENABLED:-false}"
FEISHU_CLI="${FEISHU_CLI:-lark-cli}"
FEISHU_IDENTITY="${FEISHU_IDENTITY:-ctxbridge}"
FEISHU_RELAY_CHAT_ID="${FEISHU_RELAY_CHAT_ID:-}"

resolve_session() { tmux has-session -t "$1" 2>/dev/null; }

# --------------------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------------------
require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

validate_python() {
  local detail
  detail=$("$CTXBRIDGE_PY" - <<'PY' 2>/dev/null || true
import sqlite3, sys
print("python %s, sqlite %s" % (sys.version.split()[0], sqlite3.sqlite_version))
if sqlite3.sqlite_version_info < (3, 38):
    raise SystemExit(1)
PY
)
  if [ -z "$detail" ]; then
    die "CTXBRIDGE_PY=$CTXBRIDGE_PY has no SQLite >= 3.38. Agent orchestrator databases use JSON
       '->>' generated columns, so the collector needs a newer SQLite. Point CTXBRIDGE_PY at an
       interpreter that ships one (check: \$CTXBRIDGE_PY -c 'import sqlite3; print(sqlite3.sqlite_version)')."
  fi
  log "interpreter: $CTXBRIDGE_PY ($detail)"
}

CLI=()
# Soft variant: return 1 instead of exiting, so `status` never fails on a broken deployment.
resolve_cli_soft() {
  if [ -n "$CTXBRIDGE_BIN" ]; then
    read -r -a CLI <<< "$CTXBRIDGE_BIN"
    log "using configured ctxbridge: $CTXBRIDGE_BIN"
    return 0
  fi
  if (cd "$REPO_ROOT" && "$CTXBRIDGE_PY" -c 'import context_bridge.cli' >/dev/null 2>&1); then
    CLI=("$CTXBRIDGE_PY" "-m" "context_bridge.cli")
    log "using: $CTXBRIDGE_PY -m context_bridge.cli"
    return 0
  fi
  if command -v ctxbridge >/dev/null 2>&1; then
    CLI=("ctxbridge")
    warn "using ctxbridge from PATH; make sure that binary runs under Python with SQLite >= 3.38"
    return 0
  fi
  return 1
}

resolve_cli() {
  resolve_cli_soft && return 0
  die "context_bridge is not importable by $CTXBRIDGE_PY.
       Run: $CTXBRIDGE_PY -m pip install -e \"$REPO_ROOT\"
       (or set CTXBRIDGE_BIN to an existing ctxbridge executable)."
}

cli_words() {
  local out="" word
  for word in "${CLI[@]}"; do out+="$(printf '%q' "$word") "; done
  printf '%s' "${out% }"
}

ensure_package() {
  # The daemons always run with cwd=$REPO_ROOT and `python -m context_bridge.cli`, so an
  # importable-from-cwd package is enough. An editable install is what makes `ctxbridge`
  # available on PATH for bots; it is attempted once and never fatal (offline hosts).
  if (cd "$REPO_ROOT" && "$CTXBRIDGE_PY" -c 'import context_bridge.cli' >/dev/null 2>&1); then
    :
  else
    die "context_bridge is not importable by $CTXBRIDGE_PY from $REPO_ROOT"
  fi
  [ "$CTXBRIDGE_INSTALL" = 0 ] && return 0
  if (cd / && "$CTXBRIDGE_PY" -c 'import context_bridge' >/dev/null 2>&1); then
    return 0
  fi
  log "installing context-bridge into $CTXBRIDGE_PY (editable, best effort)"
  if is_dry; then
    printf '[dry-run] %q -m pip install -e %q\n' "$CTXBRIDGE_PY" "$REPO_ROOT"
    return 0
  fi
  if "$CTXBRIDGE_PY" -m pip install -e "$REPO_ROOT" --no-build-isolation --quiet 2>/dev/null \
    || "$CTXBRIDGE_PY" -m pip install -e "$REPO_ROOT" --quiet 2>/dev/null; then
    log "editable install OK: $(dirname "$CTXBRIDGE_PY")/ctxbridge"
  else
    warn "editable install failed (offline?); daemons still run via 'python -m' with cwd=$REPO_ROOT"
  fi
}

# --------------------------------------------------------------------------------------
# config.toml generation from deploy.env
# --------------------------------------------------------------------------------------
toml_str() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }
trim() { local v="$1"; v="${v#"${v%%[![:space:]]*}"}"; v="${v%"${v##*[![:space:]]}"}"; printf '%s' "$v"; }

bot_var() {
  local var
  var="BOT_$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9_' '_')_$2"
  if [[ -v $var ]]; then printf '%s' "${!var}"; fi
}

bot_label() {
  local id="$1" label
  label=$(bot_var "$id" LABEL)
  if [ -n "$label" ]; then printf '%s' "$label"; return; fi
  printf '%s%s' "$(printf '%s' "${id:0:1}" | tr '[:lower:]' '[:upper:]')" "${id:1}"
}

config_content() {
  printf '# Context Bridge deployment config — generated by scripts/deploy.sh, local only.\n'
  printf '# Source of truth: the untracked deploy.env. Never commit this file.\n'
  printf '# Generated: %s\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

  printf '[hub]\n'
  printf 'dir = "%s"\n' "$(toml_str "$HUB_DIR")"
  if [ -n "${HUB_REMOTE:-}" ]; then
    printf 'remote = "%s"\n' "$(toml_str "$HUB_REMOTE")"
  fi
  printf 'commit = %s\n' "${HUB_COMMIT:-true}"
  printf 'auto_push = %s\n\n' "${HUB_AUTO_PUSH:-false}"

  printf '[server]\n'
  printf 'host = "%s"\n' "$(toml_str "$BIND_HOST")"
  printf 'port = %s\n' "$PORT"
  if [ -n "${SERVER_TOKEN:-}" ]; then
    printf '# bearer token read from deploy.env; keep it out of the repository\n'
    printf 'token = "%s"\n' "$(toml_str "$SERVER_TOKEN")"
  fi
  printf '\n'

  printf '[redaction]\n'
  printf 'enabled = true\n'
  printf 'mask_private_ips = true\n'
  if [ -n "$MASK_INTERNAL_DOMAINS" ]; then
    local domains="" domain
    IFS=',' read -r -a _domains <<< "$MASK_INTERNAL_DOMAINS"
    for domain in "${_domains[@]}"; do
      domain=$(trim "$domain")
      [ -n "$domain" ] || continue
      domains+="\"$(toml_str "$domain")\", "
    done
    printf 'mask_internal_domains = [%s]\n' "${domains%, }"
  fi
  printf '\n'

  if [ "$FEISHU_ENABLED" = true ]; then
    [ -n "$FEISHU_RELAY_CHAT_ID" ] || die "FEISHU_ENABLED=true requires FEISHU_RELAY_CHAT_ID"
    printf '[feishu]\n'
    printf 'enabled = true\n'
    printf 'cli = "%s"\n' "$(toml_str "$FEISHU_CLI")"
    printf 'identity = "%s"\n' "$(toml_str "$FEISHU_IDENTITY")"
    printf 'relay_chat_id = "%s"\n\n' "$(toml_str "$FEISHU_RELAY_CHAT_ID")"
    local bot open_id
    for bot in "${BOT_IDS[@]}"; do
      open_id=$(bot_var "$bot" OPEN_ID)
      [ -n "$open_id" ] || continue
      printf '[feishu.targets.%s]\nopen_id = "%s"\n\n' "$bot" "$(toml_str "$open_id")"
    done
  fi

  local colors=( "#7c5cff" "#2fc8b5" "#f0a13b" "#ff6b8a" "#6aa9ff" )
  local emojis=( "🟣" "🩵" "🟠" "🔴" "🔵" )
  local bot state label profile color emoji sessions idx=0
  for bot in "${BOT_IDS[@]}"; do
    state=$(bot_var "$bot" STATE_DIR)
    [ -n "$state" ] || die "BOTS lists '$bot' but BOT_$(printf '%s' "$bot" | tr '[:lower:]' '[:upper:]')_STATE_DIR is not set"
    label=$(bot_label "$bot")
    profile=$(bot_var "$bot" PROFILE)
    [ -n "$profile" ] || profile=$bot
    color=$(bot_var "$bot" COLOR)
    [ -n "$color" ] || color=${colors[$((idx % ${#colors[@]}))]}
    emoji=$(bot_var "$bot" EMOJI)
    [ -n "$emoji" ] || emoji=${emojis[$((idx % ${#emojis[@]}))]}
    sessions=$(bot_var "$bot" SESSIONS_ROOT)
    [ -n "$sessions" ] || sessions=$SESSIONS_ROOT

    printf '[bots.%s]\n' "$bot"
    printf 'label = "%s"\n' "$(toml_str "$label")"
    printf 'profile = "%s"\n' "$(toml_str "$profile")"
    printf 'state_dir = "%s"\n' "$(toml_str "$state")"
    if [ -n "$sessions" ]; then
      printf 'sessions_root = "%s"\n' "$(toml_str "$sessions")"
    fi
    printf 'color = "%s"\n' "$(toml_str "$color")"
    printf 'emoji = "%s"\n\n' "$(toml_str "$emoji")"
    idx=$((idx + 1))
  done
}

validate_toml() {
  "$CTXBRIDGE_PY" - "$1" <<'PY'
import sys, tomllib
with open(sys.argv[1], "rb") as fh:
    tomllib.load(fh)
PY
}

write_config() {
  if [ -f "$CONFIG" ] && [ "$FORCE_CONFIG" = 0 ]; then
    log "config exists, leaving it alone: $CONFIG (use --force-config to regenerate)"
    return 0
  fi
  [ -n "$BOTS" ] || die "BOTS is not set in the deployment env (space-separated bot ids, e.g. BOTS=\"opus sonnet fable\")"
  read -r -a BOT_IDS <<< "$BOTS"
  [ "${#BOT_IDS[@]}" -gt 0 ] || die "BOTS is empty"
  local bot
  for bot in "${BOT_IDS[@]}"; do
    [[ "$bot" =~ ^[A-Za-z0-9_-]+$ ]] || die "invalid bot id '$bot' (use letters, digits, dash, underscore)"
  done

  local tmp
  if is_dry; then
    tmp=$(mktemp)
  else
    run mkdir -p "$(dirname -- "$CONFIG")"
    tmp=$(mktemp "$CONFIG.XXXXXX")
  fi
  config_content > "$tmp"
  if ! validate_toml "$tmp" 2>/dev/null; then
    rm -f "$tmp"
    die "generated config did not parse as TOML; check deploy.env values for stray quotes"
  fi
  if is_dry; then
    printf '[dry-run] would write %s (%s lines, TOML valid)\n' "$CONFIG" "$(wc -l < "$tmp")"
    rm -f "$tmp"
    return 0
  fi
  mv "$tmp" "$CONFIG"
  chmod 600 "$CONFIG"
  log "wrote config: $CONFIG"
}

# --------------------------------------------------------------------------------------
# hub + skill install
# --------------------------------------------------------------------------------------
init_hub() {
  run mkdir -p "$HUB_DIR"
  local had_hub_config=0
  [ -f "$HUB_DIR/config.toml" ] && had_hub_config=1
  if [ -f "$HUB_DIR/manifest.json" ]; then
    log "hub already initialized: $HUB_DIR"
    return 0
  fi
  log "initializing hub repository: $HUB_DIR"
  run "${CLI[@]}" init --dir "$HUB_DIR"
  if [ "$had_hub_config" = 0 ] && [ "$CONFIG" != "$HUB_DIR/config.toml" ] && [ -f "$HUB_DIR/config.toml" ]; then
    warn "removing starter config written by 'ctxbridge init'; deployment config is $CONFIG"
    run rm -f "$HUB_DIR/config.toml"
  fi
}

install_skill() {
  [ -d "$SKILL_SRC" ] || { warn "skill source not found: $SKILL_SRC"; return 0; }
  run mkdir -p "$(dirname -- "$SKILLS_DIR")"
  if [ "$SKILL_MODE" = copy ]; then
    run mkdir -p "$SKILLS_DIR"
    run cp -a "$SKILL_SRC/." "$SKILLS_DIR/"
    log "DSH skill installed (copy): $SKILLS_DIR"
    return 0
  fi
  if [ -e "$SKILLS_DIR" ] && [ ! -L "$SKILLS_DIR" ] && [ -d "$SKILLS_DIR" ]; then
    warn "$SKILLS_DIR exists and is a real directory; set SKILL_MODE=copy to refresh it, or remove it"
    return 0
  fi
  run ln -sfn "$SKILL_SRC" "$SKILLS_DIR"
  log "DSH skill installed (symlink): $SKILLS_DIR -> $SKILL_SRC"
}

# --------------------------------------------------------------------------------------
# tmux services
# --------------------------------------------------------------------------------------
write_wrappers() {
  run mkdir -p "$LOG_DIR"
  local sync_body serve_body name
  sync_body="exec $(cli_words) --config $(printf '%q' "$CONFIG") sync --loop $(printf '%q' "$SYNC_INTERVAL")"
  serve_body="exec $(cli_words) --config $(printf '%q' "$CONFIG") serve --host $(printf '%q' "$BIND_HOST") --port $(printf '%q' "$PORT")"

  write_wrapper "$LOG_DIR/sync.sh" "$sync_body"
  write_wrapper "$LOG_DIR/serve.sh" "$serve_body"
}

write_wrapper() {
  local file="$1" body="$2"
  if is_dry; then
    printf '[dry-run] write %s: %s\n' "$file" "$body"
    return 0
  fi
  {
    printf '#!/usr/bin/env bash\nset -euo pipefail\n'
    if [ -n "$PATH_PREFIX" ]; then
      printf 'export PATH=%q:$PATH\n' "$PATH_PREFIX"
    fi
    for name in $SERVICE_ENV; do
      if [[ -v $name ]]; then printf 'export %s=%q\n' "$name" "${!name}"; fi
    done
    printf 'cd %q\n' "$REPO_ROOT"
    printf '%s\n' "$body"
  } > "$file"
  chmod +x "$file"
}

start_session() {
  local name="$1" script="$2" logfile="$3"
  local cmd
  cmd="exec /bin/bash $(printf '%q' "$script") >> $(printf '%q' "$logfile") 2>&1"
  if resolve_session "$name"; then
    log "restarting tmux session: $name"
    run tmux kill-session -t "$name"
    is_dry || sleep 0.3
  fi
  log "starting tmux session: $name"
  run tmux new-session -d -s "$name" -c "$REPO_ROOT" "$cmd"
}

health_body() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 3 "$HEALTH_URL"
    return $?
  fi
  "$CTXBRIDGE_PY" - "$HEALTH_URL" <<'PY'
import sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=3) as resp:
    sys.stdout.write(resp.read().decode("utf-8", "replace"))
PY
}

tail_log() {
  local file="$1"
  [ -f "$file" ] || return 0
  printf -- '--- %s (last 20 lines) ---\n' "$file" >&2
  tail -n 20 "$file" >&2 || true
}

wait_health() {
  if is_dry; then
    log "[dry-run] would wait up to ${HEALTH_TIMEOUT}s for $HEALTH_URL"
    return 0
  fi
  local i=0
  while [ "$i" -lt "$HEALTH_TIMEOUT" ]; do
    if health_body >/dev/null 2>&1; then
      log "health check OK: $HEALTH_URL"
      return 0
    fi
    if ! resolve_session "$SERVE_SESSION"; then
      warn "serve session exited before becoming healthy"
      tail_log "$LOG_DIR/serve.log"
      return 1
    fi
    sleep 1
    i=$((i + 1))
  done
  warn "health check timed out after ${HEALTH_TIMEOUT}s"
  tail_log "$LOG_DIR/serve.log"
  return 1
}

# --------------------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------------------
cmd_status() {
  printf '\n== Context Bridge ==\n'
  printf '  hub:    %s\n' "$HUB_DIR"
  printf '  config: %s\n' "$CONFIG"
  printf '  env:    %s\n' "${DEPLOY_ENV:-<none>}"
  if [ -n "$PUBLIC_URL" ]; then
    printf '  url:    %s\n' "$PUBLIC_URL"
  else
    printf '  url:    http://localhost:%s/ (local; set PUBLIC_URL for the external address)\n' "$PORT"
  fi
  printf '\n  services (tmux):\n'
  local name
  for name in "$SYNC_SESSION" "$SERVE_SESSION"; do
    if command -v tmux >/dev/null 2>&1 && resolve_session "$name"; then
      printf '    %-18s running\n' "$name"
    else
      printf '    %-18s stopped\n' "$name"
    fi
  done
  printf '\n  health: '
  local body
  if body=$(health_body 2>/dev/null) && [ -n "$body" ]; then
    printf 'OK\n'
    if command -v "$CTXBRIDGE_PY" >/dev/null 2>&1; then
      printf '%s\n' "$body" | "$CTXBRIDGE_PY" -m json.tool 2>/dev/null | sed 's/^/    /' || printf '    %s\n' "$body"
    else
      printf '    %s\n' "$body"
    fi
  else
    printf 'unreachable (%s)\n' "$HEALTH_URL"
  fi
  if resolve_cli_soft >/dev/null 2>&1 && [ -f "$CONFIG" ]; then
    local hub_status
    if hub_status=$("${CLI[@]}" --config "$CONFIG" status --json 2>/dev/null); then
      printf '\n  hub status:\n'
      printf '%s\n' "$hub_status" | "$CTXBRIDGE_PY" -m json.tool 2>/dev/null | sed 's/^/    /' || printf '    %s\n' "$hub_status"
    fi
  fi
  printf '\n  logs: %s/{sync,serve}.log\n\n' "$LOG_DIR"
}

cmd_logs() {
  local file
  for file in "$LOG_DIR/sync.log" "$LOG_DIR/serve.log"; do
    if [ -f "$file" ]; then
      printf '== %s ==\n' "$file"
      tail -n "$LOG_LINES" "$file" || true
      printf '\n'
    else
      printf '== %s == (missing)\n\n' "$file"
    fi
  done
}

cmd_stop() {
  require_cmd tmux
  local name
  for name in "$SYNC_SESSION" "$SERVE_SESSION"; do
    if resolve_session "$name"; then
      run tmux kill-session -t "$name"
      log "stopped tmux session: $name"
    else
      log "not running: $name"
    fi
  done
}

cmd_config() {
  validate_python
  write_config
  if [ "$PRINT_CONFIG" = 1 ] && [ -f "$CONFIG" ]; then
    printf -- '--- %s ---\n' "$CONFIG"
    sed -e 's/^token = ".*/token = "[REDACTED]"/' "$CONFIG"
  fi
  printf '[deploy] config ready: %s\n' "$CONFIG"
  printf '[deploy] serve manually: %s -m context_bridge.cli --config %s serve\n' "$CTXBRIDGE_PY" "$CONFIG"
}

cmd_start() {
  require_cmd git
  require_cmd tmux
  validate_python
  resolve_cli
  ensure_package
  write_config
  init_hub
  install_skill
  write_wrappers
  start_session "$SYNC_SESSION" "$LOG_DIR/sync.sh" "$LOG_DIR/sync.log"
  start_session "$SERVE_SESSION" "$LOG_DIR/serve.sh" "$LOG_DIR/serve.log"
  if ! wait_health; then
    warn "deployment incomplete; sessions were left running for inspection"
    warn "inspect with: $SCRIPT_DIR/deploy.sh status  |  logs"
    exit 1
  fi
  cmd_status
  printf '[deploy] done.\n'
  if is_dry; then
    printf '[deploy] dry run: nothing was started or written.\n'
  else
    printf '[deploy] console: %s\n' "${PUBLIC_URL:-http://localhost:$PORT/}"
    printf '[deploy] stop:    %s stop\n' "$SCRIPT_DIR/deploy.sh"
  fi
}

case "$ACTION" in
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_stop; cmd_start ;;
  status) cmd_status ;;
  logs) cmd_logs ;;
  config) cmd_config ;;
  *) usage >&2; exit 2 ;;
esac
