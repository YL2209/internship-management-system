#!/bin/bash
set -e

# ============================================================
# 实习数据管理系统 — Docker 容器入口脚本
# ============================================================

echo "============================================"
echo "  实习数据管理系统守护进程 (Docker)"
echo "============================================"
echo "配置:   /app/config.json"
echo "缓存:   ${CACHE_DIR:-/app/cache}"
echo "日志:   ${LOG_DIR:-/app/logs}"
echo "时区:   ${TZ:-Asia/Shanghai}"
echo "启动:   $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "用户:   $(whoami) (UID=$(id -u))"
echo "============================================"

# ------------------------------------------------------------------
# 配置文件校验
# ------------------------------------------------------------------
if [ ! -f /app/config.json ]; then
    echo ""
    echo "[错误] 未找到 /app/config.json"
    echo "  请通过 volume 挂载: -v ./config.json:/app/config.json"
    echo ""
    exit 1
fi

# 校验 JSON 合法性
if ! python -c "import json; json.load(open('/app/config.json'))" 2>/dev/null; then
    echo ""
    echo "[错误] /app/config.json 格式无效，请检查 JSON 语法"
    echo ""
    exit 1
fi

# ------------------------------------------------------------------
# Schedule 配置检查
# ------------------------------------------------------------------
SCHEDULE_CHECK=$(python -c "
import json
try:
    cfg = json.load(open('/app/config.json'))
except Exception:
    print('invalid_json')
    exit(0)
s = cfg.get('schedule', {})
if not isinstance(s, dict):
    print('no_schedule')
else:
    enabled = s.get('enabled', True)
    tasks = s.get('tasks', [])
    if not isinstance(tasks, list): tasks = []
    print('ok' if enabled and tasks else ('disabled' if not enabled else 'no_tasks'))
    print('|' + str(len(tasks)) + ' tasks')
" 2>/dev/null)

STATUS=$(echo "$SCHEDULE_CHECK" | head -1)
TASK_INFO=$(echo "$SCHEDULE_CHECK" | tail -1)

case "$STATUS" in
    ok|"ok "*)
        echo "[OK] 定时任务已就绪 (${TASK_INFO})"
        ;;
    disabled|"disabled"*)
        echo "[WARN] schedule.enabled=false，定时任务已关闭"
        echo "      守护进程将空转等待配置更改"
        ;;
    no_tasks|"no_tasks"*)
        echo "[WARN] schedule.tasks 为空，请先在 Web 管理后台添加任务"
        echo "      守护进程将空转等待"
        ;;
    no_schedule|invalid_json|"no_schedule"*)
        echo "[ERROR] config.json 缺少有效的 schedule 配置段"
        exit 1
        ;;
    *)
        echo "[ERROR] 配置检查异常: $SCHEDULE_CHECK"
        exit 1
        ;;
esac

# ------------------------------------------------------------------
# 检查关键目录权限
# ------------------------------------------------------------------
for dir in /app/cache /app/logs; do
    if [ -d "$dir" ] && [ ! -w "$dir" ]; then
        echo "[WARN] $dir 目录不可写，请检查 volume 挂载权限"
    fi
done

echo "============================================"

# ------------------------------------------------------------------
# exec 替换 shell，使 Python 成为 PID 1 接收 SIGTERM
# ------------------------------------------------------------------
exec python /app/run.py --config /app/config.json
