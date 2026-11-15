FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 STATIC_MANIFEST=true
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt
ARG APP_VERSION=development
ARG VCS_REF=unknown
LABEL org.opencontainers.image.source="https://github.com/gfavre/probe" \
      org.opencontainers.image.title="HTTP monitoring" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"
ENV APP_VERSION=${APP_VERSION}
COPY . .
RUN DJANGO_SECRET_KEY=build-only POSTGRES_PASSWORD=build-only python manage.py collectstatic --noinput \
    && useradd --uid 10001 --create-home app
USER app
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "2", "--access-logfile", "-", "--access-logformat", "%(h)s %(m)s %(s)s %(L)s"]
