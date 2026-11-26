FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data
RUN groupadd -g 10001 app && useradd -u 10001 -g app -M -s /usr/sbin/nologin app && mkdir -p /data /app && chown app:app /data /app
WORKDIR /app
COPY --chown=app:app app.py /app/app.py
USER app
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=4)"
CMD ["python", "app.py"]
