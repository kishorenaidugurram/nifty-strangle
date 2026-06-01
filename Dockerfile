FROM python:3.11-slim

WORKDIR /app

# Install deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files (bot.py for order placement, railway_monitor.py for WS)
COPY bot.py .
COPY railway_monitor.py .
# Copy bot.py's dependencies
COPY strangle_calculator.py .

# Railway sets PORT env var automatically
ENV PYTHONUNBUFFERED=1

# Run the WebSocket monitor
CMD ["python", "railway_monitor.py"]
