FROM python:3.10-slim

WORKDIR /app

# Install system deps for building
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY bot.py .
COPY railway_monitor.py .
COPY strangle_calculator.py .

ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "railway_monitor.py"]
