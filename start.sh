#!/bin/bash
# Inferolli entrypoint — THE template start command (runs at boot as pid-1's child).
# Waits for the launcher to persist /bot/.env, then restores + runs everything.
set -u
cd /bot

# Every stage logs here. Before this, the boot was invisible: start.sh wrote to
# the start command's stdout, which nothing captures, so a sandbox that never
# booted the bot looked exactly like one that was still starting.
SLOG=/bot/bot.log
slog() { echo "[start $(date -u +%H:%M:%S)] $*" >> "$SLOG"; }
slog "start.sh up: .env=$([ -f /bot/.env ] && echo present || echo absent), env vars=$(env | wc -l)"

# --- ONE start.sh per box ---
# A resume re-runs this command even when the previous tree is still alive.
# On 2026-11-22 that left FIVE start.sh trees and TWO bot.py processes on one
# token: both answered every message, so the server saw double replies. The
# bot refuses a second gateway itself (single_instance.py); this stops the
# second supervisor/watchdog from existing at all. flock is released by the
# kernel when the holder dies, so a crashed tree never blocks the next boot.
if command -v flock >/dev/null 2>&1; then
  exec 200>/tmp/inferolli-start.lock
  if ! flock -n 200; then
    slog "a start.sh tree already owns this box — this invocation exits"
    echo "[start] another start.sh is already running; not starting a second stack"
    exit 0
  fi
fi

rebuild_env_from_environment() {
  # A sandbox created by a PREDECESSOR (the 24h reincarnation path) has no
  # launcher to write /bot/.env, and the canonical copy lives in S3 — which we
  # cannot reach without credentials. Chicken and egg. Creation envs DO reach
  # the start command, so rebuild .env from them; the S3 restore below then
  # replaces it with the real one.
  echo "[start] no .env file — rebuilding it from the environment"
  : > /bot/.env
  for k in DISCORD_BOT_TOKEN INFERENCEPORT_API_KEY E2B_API_KEY AI_GATEWAY_KEY \
           S3_ENDPOINT S3_BUCKET S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY \
           TONEARM_URL TONEARM_API_KEY TINYFISH_API_KEY; do
    v="$(printenv "$k" 2>/dev/null || true)"
    [ -n "$v" ] && printf '%s=%s\n' "$k" "$v" >> /bot/.env
  done
}

# --- if we booted with credentials but no file, use them NOW. Waiting ten
#     minutes for a launcher that does not exist was a real outage: the
#     reincarnated sandbox never started at all.
#
#     The test is "are there credentials in the environment at ALL", not "is
#     there a Discord token": a drill passed only the S3 keys, the stricter
#     gate said no, and the sandbox waited out the full loop and idled. ---
have_any_creds() {
  for k in DISCORD_BOT_TOKEN E2B_API_KEY S3_ACCESS_KEY_ID S3_BUCKET; do
    printenv "$k" >/dev/null 2>&1 && return 0
  done
  return 1
}
if [ ! -f /bot/.env ] && have_any_creds; then
  rebuild_env_from_environment
fi
slog "after bootstrap: .env=$(wc -c < /bot/.env 2>/dev/null || echo 0) bytes"

# --- otherwise wait for the launcher (it writes /bot/.env right after create;
#     dashboard-started sandboxes never get one and idle out below) ---
WAITED=0
while [ ! -f /bot/.env ] && [ "$WAITED" -lt 120 ]; do
  sleep 5
  WAITED=$((WAITED + 5))
done
[ -f /bot/.env ] || rebuild_env_from_environment

# --- an EMPTY .env means the secrets may still be in flight. A keeper-created
#     sandbox gets /bot/.env written seconds after create, but the SDK's
#     connect can retry for minutes before the sandbox accepts files (a real
#     drill measured 4.5 minutes). Idling at the first empty check would
#     quietly kill exactly the sandbox the keeper just paid for. ---
WAITED=0
while [ ! -s /bot/.env ] && [ "$WAITED" -lt 600 ]; do
  sleep 5
  WAITED=$((WAITED + 5))
done
if [ ! -s /bot/.env ]; then
  echo "[start] no credentials arrived in 10 minutes — idling"
  exec sleep infinity
fi
set -a; . /bot/.env; set +a

# --- restore the brain + the canonical .env from S3, BEFORE anything needs the
#     real secrets (a reincarnated sandbox bootstraps a minimal .env above and
#     only gets DISCORD_BOT_TOKEN / provider keys from this restore) ---
slog "restoring brain from s3..."
python -c "from cogs.backup import restore_latest; print('[start]', restore_latest())" 2>&1 | tail -1
set -a; . /bot/.env; set +a
slog "brain restored: .env=$(wc -c < /bot/.env 2>/dev/null || echo 0) bytes"

# --- its own source, versioned: the canonical repo is public, so a plain clone
#     needs no credentials. /bot is the RUNNING copy — live, unversioned, and
#     it is what is actually executing; this clone is the reference copy it can
#     git log / blame / diff against. Never fatal: a sandbox without network
#     to github still boots the bot. ---
mkdir -p /workspace
slog "cloning the source repo..."
if [ -d /workspace/inferolli/.git ]; then
  git -C /workspace/inferolli pull --ff-only >/dev/null 2>&1 \
    || git -C /workspace/inferolli fetch --depth 100 origin >/dev/null 2>&1 || true
else
  rm -rf /workspace/inferolli
  git clone --depth 100 https://github.com/InferencePort-AI/inferolli.git \
    /workspace/inferolli >/dev/null 2>&1 || true
fi
slog "source repo: $(git -C /workspace/inferolli log --oneline -1 2>/dev/null || echo 'clone unavailable')"

# --- the desktop display: Xvfb on :99, XFCE4 riding on it ---
Xvfb :99 -screen 0 1440x900x24 -nolisten tcp > /dev/null 2>&1 &
export DISPLAY=:99
sleep 1.5
(dbus-launch --exit-with-session > /tmp/dbus.env 2>/dev/null; \
  . /tmp/dbus.env 2>/dev/null; xfwm4 & xfdesktop & xfce4-panel & xfce4-terminal &) \
  > /dev/null 2>&1

# --- opencode server: the bot's persistent agent sessions (per-channel threads) ---
export XDG_DATA_HOME=/bot/data/xdg   # opencode session storage lives in the backed-up brain
mkdir -p "$XDG_DATA_HOME"
# opencode needs the provider keys — the secure envs don't reach the start command,
# so source the restored .env (it's there: the launcher persists it at boot)
set -a; . /bot/.env; set +a
# opencode mints its own password each start and prints it to the serve log; the
# client reads it from there. Do NOT export a fixed one — a stale value means
# every request 401s and the primary reply engine dies quietly.
unset OPENCODE_SERVER_PASSWORD
(nohup opencode serve --port 4096 > /tmp/opencode-serve.log 2>&1 &)
slog "opencode serve launched"
for _ in $(seq 1 20); do
  grep -q 'server password' /tmp/opencode-serve.log 2>/dev/null && break
  sleep 0.5
done

# --- tonearm: the music studio lives IN this sandbox ---
# It used to be a separate e2b sandbox whose URL went stale whenever that box
# paused — /play dialed a dead host and voice parties stayed silent (the bot's
# preflight needs a live pump). In-sandbox: one thing to keep alive, one URL.
TONearm_DIR=/home/user/tonearm
TONearm_CFG=/home/user/.config/tonearm
start_tonearm() {
  [ -d "$TONearm_DIR" ] || return 0
  if [ ! -f "$TONearm_CFG/chrome-profile/Default/Cookies" ]; then
    slog "tonearm: no Apple Music session profile — studio skipped"
    return 0
  fi
  curl -s -m 2 http://127.0.0.1:8188/health >/dev/null 2>&1 && return 0
  # pulse needs a runtime dir it can OWN — /run/user/1000 doesn't exist in the
  # sandbox, and without it chrome plays to a dead daemon (digital silence)
  mkdir -p /tmp/pulse-runtime && chmod 700 /tmp/pulse-runtime 2>/dev/null
  # ENABLE_WRITES lets /playlist create and edit the playlists the BOT made
  # (the cog hard-refuses to touch any playlist it didn't create).
  ( cd "$TONearm_DIR" && XDG_RUNTIME_DIR=/tmp/pulse-runtime \
      TONEARM_AUDIO=1 TONEARM_CHROME_BIN=chromium TONEARM_PORT=8188 \
      TONEARM_ENABLE_WRITES=1 \
      setsid nohup python3 -m tonearm >> /tmp/tonearm.log 2>&1 & )
  slog "tonearm: studio launched"
}
start_tonearm

# --- our own runtime window: do not rely on the VPS being awake ---
# `e2b sandbox resume` grants only 5 minutes by default, which is less than a
# cold boot; without this the sandbox pauses again before the bot can ever
# extend it, and from the outside that looks like the bot randomly stopping.
export E2B_KEEPALIVE=1
if [ -z "${E2B_SANDBOX_ID:-}" ] || [ "${E2B_SANDBOX_ID}" = "e2b.local" ]; then
  if [ -s /bot/.e2b-sandbox-id ]; then
    export E2B_SANDBOX_ID="$(cat /bot/.e2b-sandbox-id)"
  fi
else
  # remember it: the bot's keepalive and opencode both read this file
  printf '%s' "$E2B_SANDBOX_ID" > /bot/.e2b-sandbox-id
fi
if [ -n "${E2B_SANDBOX_ID:-}" ] && [ -n "${E2B_API_KEY:-}" ]; then
  python3 - <<'EOPY' >> /bot/bot.log 2>&1
import json, os, urllib.request
sid, key = os.environ["E2B_SANDBOX_ID"], os.environ["E2B_API_KEY"]
req = urllib.request.Request(
    f"https://api.e2b.app/sandboxes/{sid}/timeout",
    data=json.dumps({"timeout": 3600}).encode(),
    headers={"X-API-KEY": key, "Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        print(f"[start] e2b window extended at boot (HTTP {r.status})")
except Exception as e:
    print(f"[start] e2b window extension FAILED: {e}")
EOPY
else
  slog "no sandbox id or API key — cannot extend the e2b window"
fi

# --- the stack watchdog: any dead piece gets revived (children of the start
#     command survive; nothing exec-launched does — so this is the healer) ---
ensure_tunnel() {
  # A quick tunnel DIES whenever the sandbox pauses (cloudflare reaps it), and
  # on resume cloudflared retries the dead tunnel id forever —
  # "Unauthorized: Tunnel not found" — while pgrep reports it perfectly alive.
  # So the test is whether the tunnel's URL still answers /health, not whether
  # the process exists. After any pause the watchdog mints a fresh URL here.
  local tun launch grace
  if ! pgrep -x cloudflared >/dev/null 2>&1; then
    launch=1
  else
    tun="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/cloudflared.log 2>/dev/null | tail -1)"
    if [ -n "$tun" ]; then
      if curl -s --max-time 6 "$tun/health" 2>/dev/null | grep -q '"ok"'; then
        launch=0
      else
        launch=1
      fi
    else
      # no URL yet: fine for the first ~90s of a launch (handshake), dead after
      grace=$(( $(date +%s) - $(cat /tmp/cloudflared.started 2>/dev/null || echo 0) ))
      if [ "$grace" -gt 90 ]; then launch=1; else launch=0; fi
    fi
  fi
  if [ "$launch" = 1 ]; then
    pkill -x cloudflared 2>/dev/null
    sleep 1
    : > /tmp/cloudflared.log
    date +%s > /tmp/cloudflared.started
    (nohup cloudflared tunnel --url http://localhost:8080 --no-autoupdate \
      > /tmp/cloudflared.log 2>&1 &)
  fi
}

(while true; do
  sleep 30
  pgrep -f 'Xvfb :99' >/dev/null || { Xvfb :99 -screen 0 1440x900x24 -nolisten tcp > /dev/null 2>&1 & sleep 2; }
  pgrep -f 'x11vnc -display' >/dev/null || {
    (x11vnc -display :99 -forever -shared -nopw -quiet -viewonly -rfbport 5901 > /tmp/x11vnc-ro.log 2>&1 &)
    (x11vnc -display :99 -forever -shared -nopw -quiet -rfbport 5900 > /tmp/x11vnc-rw.log 2>&1 &)
  }
  pgrep -f 'uvicorn desktop_gate' >/dev/null || \
    (nohup python3 -m uvicorn desktop_gate:app --host 127.0.0.1 --port 8080 > /tmp/gate.log 2>&1 &)
  ensure_tunnel
  pgrep -f 'opencode serve' >/dev/null || \
    (nohup opencode serve --port 4096 > /tmp/opencode-serve.log 2>&1 &)
  start_tonearm
done > /dev/null 2>&1 &)

# --- run the bot under a supervisor loop (logs to /bot/bot.log) ---
slog "handing over to the supervisor"
while true; do
  echo "=== [supervisor] starting bot $(date)" >> /bot/bot.log
  python -u bot.py >> /bot/bot.log 2>&1
  code=$?
  if [ "$code" = 3 ]; then
    # the bot refused: another live process owns the token on this box. Do not
    # hammer it at 5s intervals — it will be gone (or this tree will) soon.
    echo "[supervisor] another bot holds the lock — waiting 60s" >> /bot/bot.log
    sleep 60
  else
    echo "[supervisor] bot exited with $code — restarting in 5s" >> /bot/bot.log
    sleep 5
  fi
done
