#!/usr/bin/env bash
# ============================================================================
#  Hyper Liquid Trader (Personal) — one-shot installer  [SYSTEMD EDITION]
#  Installs everything, then boots BOTH bots as systemd services so they
#  auto-start after a server reboot.  No screen required.
#
#  Run:  bash setup_systemd.sh
#  (if you already have the repo, run it from inside the repo root)
# ============================================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/matildameta/trading-bot-hl.git}"
REPO_DIR="${REPO_DIR:-trading-bot-hl}"
PY_MIN="3.10"

# ----- resolved absolute paths (used by the systemd units) -----
INSTALL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# If launched from inside the repo, INSTALL_ROOT is the trading-bot folder.
# The venv + core_engine + control_bot live directly under it.
VENV_ACTIVATE="$INSTALL_ROOT/.venv/bin/activate"
ENGINE_DIR="$INSTALL_ROOT/core_engine"
CTL_DIR="$INSTALL_ROOT/control_bot"

echo "╔══════════════════════════════════════════════════════════╗"
echo "║   Hyper Liquid Trader — Installer (systemd auto-start)   ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo "  Install root : $INSTALL_ROOT"

# ---------------------------------------------------------------------------
# 1) VITAL INPUTS (asked FIRST, before anything else)
# ---------------------------------------------------------------------------
echo
echo "▶ Step 1/6 — Enter your vital credentials"
echo "  (these values are written to config.yaml and are NEVER pushed to GitHub)"
echo

read -rsp "🔑 Hyperliquid private key (secret_key): " HL_SECRET; echo
read -rp "🌐 Default network (testnet/mainnet) [testnet]: " HL_NET; HL_NET="${HL_NET:-testnet}"
read -rsp "🤖 Telegram bot token (panel/control): " TG_PANEL; echo
read -rsp "📣 Telegram bot token (reporter): " TG_REPORT; echo
read -rp "💬 Admin Telegram chat id (numeric): " TG_CHAT; echo
read -rp "🧠 Default LLM model [anthropic/claude-sonnet-4]: " LLM_MODEL
LLM_MODEL="${LLM_MODEL:-anthropic/claude-sonnet-4}"
read -rsp "🔓 OpenRouter API key (for LLM models): " OR_KEY; echo

# ---------------------------------------------------------------------------
# 2) SYSTEM PREREQUISITES
# ---------------------------------------------------------------------------
echo; echo "▶ Step 2/6 — Checking and installing system prerequisites"
need_apt=()
command -v git >/dev/null 2>&1 || need_apt+=(git)
command -v python3 >/dev/null 2>&1 || need_apt+=(python3)
command -v gcc >/dev/null 2>&1 || need_apt+=(build-essential)
# Prefer the newest python available; on Ubuntu 22.04 only 3.10 ships by default,
# so fall back to that instead of demanding 3.12/3.11 which may be absent.
if ! command -v python3.12 >/dev/null 2>&1 && ! command -v python3.11 >/dev/null 2>&1; then
  # no 3.12/3.11 present — make sure 3.10 (the system default) is installed
  need_apt+=(python3)
fi
if [ ${#need_apt[@]} -gt 0 ]; then
  echo "  Installing: ${need_apt[*]}"
  export DEBIAN_FRONTEND=noninteractive
  # pre-seed debconf so packages like iptables-persistent never prompt
  echo "iptables-persistent iptables-persistent/autosave_v4 boolean false" | debconf-set-selections 2>/dev/null
  echo "iptables-persistent iptables-persistent/autosave_v6 boolean false" | debconf-set-selections 2>/dev/null
  sudo apt-get update -y
  sudo apt-get install -y "${need_apt[@]}"
fi
PYBIN="$(command -v python3.12 || command -v python3.11 || command -v python3)"
echo "  Python: $($PYBIN --version 2>&1)"

# ---------------------------------------------------------------------------
# 3) GET THE CODE
# ---------------------------------------------------------------------------
echo; echo "▶ Step 3/6 — Fetching the code"
if [ -d "$REPO_DIR/.git" ]; then
  echo "  repo already present, updating…"
  git -C "$REPO_DIR" pull --ff-only
  cd "$REPO_DIR"
else
  git clone "$REPO_URL" "$REPO_DIR"
  cd "$REPO_DIR"
fi
# Re-resolve install root in case we cloned into a subdir
INSTALL_ROOT="$(pwd)"
VENV_ACTIVATE="$INSTALL_ROOT/.venv/bin/activate"
ENGINE_DIR="$INSTALL_ROOT/core_engine"
CTL_DIR="$INSTALL_ROOT/control_bot"

# ---------------------------------------------------------------------------
# 4) PYTHON VENV + DEPENDENCIES
# ---------------------------------------------------------------------------
echo; echo "▶ Step 4/6 — Creating virtualenv and installing dependencies"
if [ ! -d .venv ]; then
  "$PYBIN" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r core_engine/requirements.txt -r control_bot/requirements.txt
# pandas-ta is sometimes missing on PyPI for newer pythons — fall back to git
python -c "import pandas_ta" 2>/dev/null || pip install --quiet "git+https://github.com/twopirllc/pandas-ta.git@main"

# ---------------------------------------------------------------------------
# 5) WRITE config.yaml (from example, inject secrets)
# ---------------------------------------------------------------------------
echo; echo "▶ Step 5/6 — Writing config.yaml"
write_cfg () {
  local dir="$1"
  cat > "$dir/config.yaml" <<EOF
# Auto-generated by setup_systemd.sh — DO NOT commit this file.
network: $HL_NET
hyperliquid:
  account_address: ""
  secret_key: "$HL_SECRET"
  $HL_NET:
    account_address: ""
    secret_key: "$HL_SECRET"
llm:
  default_model: "$LLM_MODEL"
  api_keys:
    openrouter: "$OR_KEY"
  request_timeout_seconds: 60
telegram:
  bot_token: "$TG_PANEL"
  chat_id: "$TG_CHAT"
EOF
  chmod 600 "$dir/config.yaml"
}
write_cfg core_engine
# control bot only needs telegram token + chat id + shared db path
cat > control_bot/config.yaml <<EOF
# Auto-generated by setup_systemd.sh — DO NOT commit this file.
telegram_bot_token: "$TG_PANEL"
allowed_chat_id: "$TG_CHAT"
shared_db_path: "../shared/bot_state.db"
network_label: $HL_NET
EOF
chmod 600 control_bot/config.yaml
echo "  config.yaml written (chmod 600)."

# ---------------------------------------------------------------------------
# 6) INSTALL + ENABLE SYSTEMD SERVICES (auto-start on reboot)
# ---------------------------------------------------------------------------
echo; echo "▶ Step 6/6 — Installing and enabling systemd services"
SYSTEMD_DIR="/etc/systemd/system"

cat > "$SYSTEMD_DIR/trading-engine.service" <<EOF
[Unit]
Description=Trading bot core engine (Hyperliquid strategy loop)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$ENGINE_DIR
ExecStart=/bin/bash -c 'cd $ENGINE_DIR && source $VENV_ACTIVATE && exec python -m src.main'
Restart=always
RestartSec=5
StartLimitIntervalSec=0
StandardOutput=append:$ENGINE_DIR/engine.log
StandardError=append:$ENGINE_DIR/engine.log

[Install]
WantedBy=multi-user.target
EOF

cat > "$SYSTEMD_DIR/trading-ctrlbot.service" <<EOF
[Unit]
Description=Trading bot control bot (Telegram command interface)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$CTL_DIR
ExecStart=/bin/bash -c 'cd $CTL_DIR && source $VENV_ACTIVATE && exec python -m src.bot'
Restart=always
RestartSec=5
StartLimitIntervalSec=0
StandardOutput=append:$CTL_DIR/control.log
StandardError=append:$CTL_DIR/control.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now trading-engine.service
systemctl enable --now trading-ctrlbot.service

echo
echo "✅ Installation complete and services enabled."
echo "  Status:     systemctl status trading-engine trading-ctrlbot"
echo "  Logs:       journalctl -u trading-engine -f   /   journalctl -u trading-ctrlbot -f"
echo "  Stop:       systemctl stop trading-engine trading-ctrlbot"
echo "  Restart:    systemctl restart trading-engine trading-ctrlbot"
echo "  Both bots auto-start after a server reboot."
