#!/usr/bin/env bash
# ============================================================================
# 本地环境快速重启脚本
# 功能：停掉现有前后端进程 → 重新启动 → 健康检查（失败自动 dump 日志定位根因）
# 退出码：0 = 前后端均就绪 / 1 = 存在未就绪服务（上方已输出失败日志尾部）
# 前提：docker 容器运行中，依赖已安装，数据库已初始化
# ⚠️ 后台 nohup 启动命令的 exit 0 ≠ 服务就绪（startup 后台异步，失败时命令早已返回），
#    必须以此脚本的自检结果（/health 200 + 端口监听）为准。
# ============================================================================
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BE_LOG="/tmp/cogni_agent_backend.log"
FE_LOG="/tmp/cogni_agent_frontend.log"

echo "============================================"
echo "  cogni_agent 本地环境重启"
echo "============================================"

# ── 1. 停掉旧进程 ──
echo "[1/3] 停止旧进程..."
for port in 8000 5173; do
    pids=$(lsof -ti :$port 2>/dev/null || true)
    if [ -n "$pids" ]; then
        echo "  端口 $port: kill $pids"
        kill $pids 2>/dev/null || true
    fi
done
sleep 1

# 确认已停
for port in 8000 5173; do
    if lsof -ti :$port >/dev/null 2>&1; then
        echo "  端口 $port 强制终止..."
        kill -9 $(lsof -ti :$port) 2>/dev/null || true
        sleep 1
    fi
done
echo "  已停止"

# ── 2. 启动服务 ──
echo "[2/3] 启动服务..."

# 后端
cd "$PROJECT_DIR"
export ZHILV_ENV=local
nohup .venv/bin/python3 -m uvicorn cogni_agent.main:app \
    --host 0.0.0.0 --port 8000 --reload \
    > "$BE_LOG" 2>&1 &
BE_PID=$!
echo "  后端 PID=$BE_PID (日志: $BE_LOG)"

# 前端
cd "$PROJECT_DIR/web"
nohup npm run dev > "$FE_LOG" 2>&1 &
FE_PID=$!
echo "  前端 PID=$FE_PID (日志: $FE_LOG)"

# ── 3. 健康检查 ──
echo "[3/3] 等待服务就绪..."
MAX_WAIT=20
elapsed=0
be_ok=false
fe_ok=false

while [ $elapsed -lt $MAX_WAIT ]; do
    # 检查后端：/health 返回 200
    if [ "$be_ok" = false ]; then
        if curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health 2>/dev/null | grep -q 200; then
            be_ok=true
            echo "  后端 :8000 ✓"
        elif ! kill -0 "$BE_PID" 2>/dev/null; then
            # 进程已退出（startup 失败/崩溃）→ 立即判失败，不再空等
            echo "  后端进程已退出（启动失败，见日志）"
            break
        fi
    fi
    # 检查前端：端口监听
    if [ "$fe_ok" = false ]; then
        if lsof -ti :5173 >/dev/null 2>&1; then
            fe_ok=true
            echo "  前端 :5173 ✓"
        elif ! kill -0 "$FE_PID" 2>/dev/null; then
            echo "  前端进程已退出（启动失败，见日志）"
            break
        fi
    fi
    if [ "$be_ok" = true ] && [ "$fe_ok" = true ]; then
        break
    fi
    sleep 1
    elapsed=$((elapsed + 1))
done

# 最终状态：失败自动 dump 错误日志，单条命令即可拿到根因，无需再手动 tail
echo ""
echo "============================================"
if [ "$be_ok" = true ]; then
    echo "  后端 :8000 ✓"
else
    echo "  后端 :8000 ✗"
    echo "  ---- $BE_LOG 尾部 40 行 ----"
    tail -n 40 "$BE_LOG" 2>/dev/null || echo "  (后端日志不存在: $BE_LOG)"
fi
if [ "$fe_ok" = true ]; then
    echo "  前端 :5173 ✓"
else
    echo "  前端 :5173 ✗"
    echo "  ---- $FE_LOG 尾部 20 行 ----"
    tail -n 20 "$FE_LOG" 2>/dev/null || echo "  (前端日志不存在: $FE_LOG)"
fi
echo "============================================"
echo "判读提示: 日志含 'Application startup failed' = 后端启动被拒（多为 sync_schema 结构体检拦下,"
echo "          需补迁移）; 'Address already in use' = 端口被旧进程占用;"
echo "          后端日志未见 'Uvicorn running on: http://0.0.0.0:8000' 一律视为未启动成功。"

if [ "$be_ok" = true ] && [ "$fe_ok" = true ]; then
    exit 0
else
    exit 1
fi
