# Расчётный сервис (solver): Python 3.11, FastAPI.
# Собирается и поднимается через docker-compose из корня heat-network.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8

WORKDIR /srv/solver
COPY requirements.txt .
# Колёса shapely/rasterio/scikit-image самодостаточны: системный GDAL не нужен
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config ./config
COPY case_cli.py validate_cli.py ./

# Проекты и результаты — на общем томе с приложением
VOLUME ["/data"]
ENV HEAT_DATA_DIR=/data/projects

EXPOSE 8010
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010", "--workers", "1"]
