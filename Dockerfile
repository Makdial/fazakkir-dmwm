# Container image for Fazakkir.
#
# Used by Northflank (and works unchanged on Render, Railway, Fly.io and any
# Docker host). imageio-ffmpeg ships its own ffmpeg binary through pip, so no
# system package installation is needed for the MP3 extraction.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# Dependencies are copied first so the layer is reused when only code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The Django project lives in the fazakkir_dmwm/ subdirectory.
WORKDIR /app/fazakkir_dmwm

# Collect the static files at build time. WhiteNoise then serves them from
# inside the container, so no separate static-file service is needed.
RUN DJANGO_SETTINGS_MODULE=fazakkir_dmwm.settings \
    DJANGO_DEBUG=0 \
    SECRET_KEY=build-only-not-used-at-runtime \
    python manage.py collectstatic --noinput \
 && python manage.py compiletranslations

EXPOSE 8000

# Applied on every start: migrate, then serve. gunicorn runs several workers so
# the app stays responsive while a background conversion is running.
CMD ["sh", "-c", "python manage.py migrate --noinput && gunicorn fazakkir_dmwm.wsgi:application --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --timeout 180 --access-logfile -"]