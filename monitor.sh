#!/bin/bash
# ─── Nifty Strangle WS Monitor — Start/Stop Script ───
# Usage: ./monitor.sh start|stop|status|logs

PROJECT_DIR="/mnt/c/Users/Admin/Documents/Claude/Projects/nifty strangle"
SCRIPT="$PROJECT_DIR/railway_monitor.py"
PID_FILE="$PROJECT_DIR/monitor.pid"
LOG_FILE="$PROJECT_DIR/monitor.log"

case "${1:-status}" in
  start)
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
      echo "❌ Monitor already running (PID: $(cat $PID_FILE))"
      exit 1
    fi
    cd "$PROJECT_DIR"
    source ~/.hermes/.env 2>/dev/null
    export ANGEL_API_KEY ANGEL_CLIENT_CODE ANGEL_PIN ANGEL_TOTP_SECRET
    nohup python3 "$SCRIPT" > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    echo "✅ Monitor started (PID: $!)"
    echo "   Logs: $LOG_FILE"
    ;;
    
  stop)
    if [ ! -f "$PID_FILE" ]; then
      echo "❌ No PID file found"
      exit 1
    fi
    kill $(cat "$PID_FILE") 2>/dev/null && echo "✅ Monitor stopped" || echo "❌ Failed to stop"
    rm -f "$PID_FILE"
    ;;
    
  status)
    if [ -f "$PID_FILE" ] && kill -0 $(cat "$PID_FILE") 2>/dev/null; then
      echo "✅ Monitor running (PID: $(cat $PID_FILE))"
      echo "   Uptime: $(ps -o etime= -p $(cat $PID_FILE) | xargs)"
    else
      echo "❌ Monitor not running"
    fi
    ;;
    
  logs)
    tail -f "$LOG_FILE"
    ;;
    
  *)
    echo "Usage: $0 {start|stop|status|logs}"
    exit 1
    ;;
esac
