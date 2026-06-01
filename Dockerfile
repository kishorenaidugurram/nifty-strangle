FROM python:3.11-slim

WORKDIR /app

# Install full build toolchain (needed for SmartApi + PyCrypto compilation)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Pre-install PyCrypto separately (it's a problematic build)
RUN pip install --no-cache-dir pycrypto==2.6.1

# Install all other Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY bot.py .
COPY railway_monitor.py .
COPY strangle_calculator.py .

ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "railway_monitor.py"]
