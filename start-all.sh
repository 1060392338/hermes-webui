#!/bin/bash
# =============================================================================
# Hermes + WebUI 一键启动脚本
# =============================================================================
# 用法：
#   ./start-all.sh              # 启动 Hermes + web_chat
#   ./start-all.sh --hermes-only  # 只启动 Hermes（不启动 web_chat）
#
# 启动后：
#   - Hermes 运行在终端前台
#   - web_chat 运行在 http://localhost:8080
#   - 用户可通过 http://localhost:8080 访问 WebUI
# =============================================================================

set -e

HERMES_DIR="$HOME/.hermes/hermes-agent"
WEB_CHAT_DIR="$HOME/.hermes/web_chat"
PYTHON="$HERMES_DIR/venv/bin/python"
WEB_CHAT_PORT=8080
WEB_CHAT_PID=""

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 检测端口是否被占用
is_port_in_use() {
    if command -v lsof &> /dev/null; then
        lsof -i :"$1" &> /dev/null
    elif command -v netstat &> /dev/null; then
        netstat -tuln 2>/dev/null | grep -q ":$1 "
    else
        # Fallback: 用 python 检测
        $PYTHON -c "import socket; s=socket.socket(); s.connect(('localhost', $1)); s.close(); exit(0)" 2>/dev/null
    fi
}

# 启动 web_chat 服务
start_web_chat() {
    if is_port_in_use $WEB_CHAT_PORT; then
        echo -e "${YELLOW}⚠ web_chat 端口 $WEB_CHAT_PORT 已被占用，跳过启动${NC}"
        echo -e "${YELLOW}  （如需重启，请先手动终止占用进程：lsof -ti :$WEB_CHAT_PORT | xargs kill）${NC}"
    else
        echo -e "${GREEN}→ 启动 web_chat 服务 (http://localhost:$WEB_CHAT_PORT)...${NC}"
        cd "$WEB_CHAT_DIR"
        $PYTHON -m uvicorn app:app --host 0.0.0.0 --port $WEB_CHAT_PORT &
        WEB_CHAT_PID=$!
        echo $WEB_CHAT_PID > /tmp/hermes_web_chat.pid

        # 等待服务就绪
        sleep 2
        if is_port_in_use $WEB_CHAT_PORT; then
            echo -e "${GREEN}  ✓ web_chat 已就绪 (PID: $WEB_CHAT_PID)${NC}"
        else
            echo -e "${YELLOW}  ⚠ web_chat 可能未启动成功，请手动检查${NC}"
        fi
    fi
}

# 停止 web_chat 服务
stop_web_chat() {
    if [ -f /tmp/hermes_web_chat.pid ]; then
        PID=$(cat /tmp/hermes_web_chat.pid)
        if kill -0 "$PID" 2>/dev/null; then
            kill "$PID" 2>/dev/null || true
            echo -e "${GREEN}✓ web_chat 已停止 (PID: $PID)${NC}"
        fi
        rm -f /tmp/hermes_web_chat.pid
    fi
}

# 主入口
main() {
    case "${1:-}" in
        --hermes-only)
            echo -e "${GREEN}→ 启动 Hermes Agent（不启动 web_chat）...${NC}"
            cd "$HERMES_DIR"
            $PYTHON -m hermes
            ;;
        --web-only)
            start_web_chat
            ;;
        --stop)
            stop_web_chat
            ;;
        *)
            # 同时启动两者
            start_web_chat

            echo ""
            echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
            echo -e "${GREEN}  Hermes Agent + WebUI 启动中...${NC}"
            echo -e "${GREEN}  WebUI: http://localhost:$WEB_CHAT_PORT${NC}"
            echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
            echo ""
            echo -e "启动成功！现在可以："
            echo -e "  1. 访问 ${GREEN}http://localhost:$WEB_CHAT_PORT${NC} 打开 WebUI"
            echo -e "  2. 或者直接在终端与 Hermes 对话"
            echo ""
            echo -e "${YELLOW}按 Ctrl+C 停止所有服务${NC}"
            echo ""

            # 启动 Hermes（前台运行）
            cd "$HERMES_DIR"
            trap 'stop_web_chat; exit 0' INT TERM
            $PYTHON -m hermes
            RET=$?

            stop_web_chat
            exit $RET
            ;;
    esac
}

main "$@"
