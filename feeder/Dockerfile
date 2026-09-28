FROM python:3.12-slim

# bluez нужен для bluetoothctl: им перезапускается залипший адаптер
# (см. ace_rh_feeder/adapter.py). Само чтение датчика идёт через bleak
# поверх системного BlueZ (D-Bus), а не через этот пакет напрямую.
RUN apt-get update && apt-get install -y --no-install-recommends bluez \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ace_rh_feeder ./ace_rh_feeder

CMD ["python", "-m", "ace_rh_feeder.main"]
