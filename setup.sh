#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ "$EUID" -ne 0 ]]; then
  echo 'sudo bash setup.sh で実行してください。'; exit 1
fi
. /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 ]]; then
  echo '対象OSはUbuntu 24.04です。'; exit 1
fi
if ! command -v docker >/dev/null; then
  apt-get update
  apt-get install -y docker.io docker-compose-v2
elif ! docker compose version >/dev/null 2>&1; then
  echo 'Docker Compose v2を既存Dockerと同じ配布元から導入してください。'; exit 1
fi
systemctl enable --now docker
if [[ ! -f .env ]]; then
  read -rsp 'Discord BOT token（入力は非表示）: ' giveaway_token
  printf '\n'
  if [[ -z "$giveaway_token" || ! "$giveaway_token" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo 'トークン形式を確認してください。'; exit 1
  fi
  umask 077
  printf 'DISCORD_TOKEN=%s\n' "$giveaway_token" > .env
  unset giveaway_token
fi
chmod 600 .env
docker compose up -d --build
echo '起動処理が完了しました。確認: sudo docker compose logs --tail=50 bot'
echo 'RAM確認: sudo docker stats --no-stream'
