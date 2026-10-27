#!/bin/bash
# ============================================================
# 一键部署「首页」tab 到线上服务器
# 用法：把本文件 scp 到服务器，执行 bash deploy-home.sh
# ============================================================
set -e

PROJECT_DIR="/opt/mt5hub"   # ← 改成你服务器上项目的实际路径
BRANCH="main"                 # ← 改成你的主分支

echo "==> 1) 拉最新代码"
cd "$PROJECT_DIR"
git pull origin "$BRANCH"

echo "==> 2) 重建并重启所有容器"
bash build/build-docker

echo "==> 3) 等 backend 健康"
for i in 1 2 3 4 5 6 7 8 9 10; do
  status=$(docker compose ps --format json backend 2>/dev/null | grep -o '"Health":"[^"]*"' | head -1 || echo "")
  if [[ "$status" == *"healthy"* ]]; then
    echo "    backend healthy ✓"
    break
  fi
  echo "    waiting... ($i/10)"
  sleep 3
done

echo "==> 4) 验证后端菜单注册表包含 home"
docker compose exec -T backend python -c "
from app.permissions import ALL_MENU_CODES
print('All menus:', ALL_MENU_CODES)
assert 'home' in ALL_MENU_CODES, 'home NOT in registry!'
print('home ✓ in registry')
"

echo "==> 5) 补全普通用户角色的 home 菜单（幂等）"
docker compose exec -T mysql mysql -u${MYSQL_USER:-mt5} -p${MYSQL_PASSWORD:-mt5pwd} ${MYSQL_DATABASE:-mt5hub} -e "
INSERT IGNORE INTO sys_role_menu (role_id, menu_code)
SELECT id, 'home' FROM sys_role WHERE code = 'user';
"

echo "==> 6) 清空 Redis 权限缓存，让旧 session 重新拉 /api/me"
docker compose exec -T redis sh -c '
  keys=$(redis-cli KEYS "principal:*")
  if [ -n "$keys" ]; then
    echo "$keys" | xargs redis-cli DEL
  fi
  echo "redis cache cleared"
'

echo ""
echo "✅ 部署完成！请："
echo "   1) 浏览器硬刷新（Ctrl+Shift+R）"
echo "   2) 重新登录"
echo "   3) 检查顶部导航第一项是不是「首页」🏠"
