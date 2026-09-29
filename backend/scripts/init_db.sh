#!/usr/bin/env bash
# 初始化 PostgreSQL：角色 + 3 库（全局/团队 demo/CEO）
# 用法：sudo bash scripts/init_db.sh
set -e

DB_USER="${DB_USER:-aip}"
DB_PASS="${DB_PASS:-aip_dev_pass}"

sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASS}' CREATEDB;
  END IF;
END
\$\$;
SQL

for db in ai_platform_tardis tardis_dept_demo_db tardis_ceo_db; do
  if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${db}'" | grep -q 1; then
    sudo -u postgres createdb -O ${DB_USER} "${db}"
    echo "created: ${db}"
  else
    echo "exists: ${db}"
  fi
done

echo "=== init_db done ==="
