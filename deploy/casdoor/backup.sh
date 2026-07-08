#!/usr/bin/env bash
# Casdoor Postgres 备份 —— pg_dump 出整库(含所有身份/组/角色/应用配置),压缩留最近 N 份。
# 丢了这个卷 = 丢全部身份数据,所以务必 cron 每日跑,并把 backups/ 同步到异地/对象存储。
#
# 用法:  deploy/casdoor/backup.sh
# cron:  0 3 * * *  /path/to/even-auth-gateway/deploy/casdoor/backup.sh >> /var/log/casdoor-backup.log 2>&1
# 恢复:  见 README「备份 / 恢复」
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && { set -a; . ./.env; set +a; }

DIR="${CASDOOR_BACKUP_DIR:-./backups}"
KEEP="${CASDOOR_BACKUP_KEEP:-14}"
DB_USER="${CASDOOR_DB_USER:-casdoor}"
DB_NAME="${CASDOOR_DB_NAME:-casdoor}"

mkdir -p "$DIR"
TS=$(date +%Y%m%d-%H%M%S)
OUT="$DIR/casdoor-$TS.sql.gz"

# 在 db 容器里跑 pg_dump —— 宿主无需装 postgres 客户端。
docker compose -f docker-compose.yml exec -T casdoor-db pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$OUT"
echo "backup -> $OUT ($(du -h "$OUT" | cut -f1))"

# 轮转:只保留最近 KEEP 份。
ls -1t "$DIR"/casdoor-*.sql.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
