# Ozone API – Open Banking Stress Test
#   docker build -t ozone-ob-stress-test .
#   docker run -d -p 127.0.0.1:8089:8089 -v ob-data:/data ozone-ob-stress-test
FROM python:3.12-slim-bookworm

# PDF reports need a headless Chromium (~400 MB). Build with --build-arg WITH_PDF=false to leave it out.
ARG WITH_PDF=true
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && if [ "$WITH_PDF" = "true" ]; then \
      pip install --no-cache-dir playwright==1.63.0 \
      && playwright install --with-deps chromium \
      && chmod -R a+rx /ms-playwright \
      && rm -rf /var/lib/apt/lists/*; \
    fi

# Application code (read-only at runtime)
COPY locustfile.py ob_core.py ob_web.py ob_brand.py report.py exemption_report.py exemption_config.yaml \
     ob_tenants.html ob_profiles.html ob_callback.html healthcheck.py ./
COPY brand ./brand

# Everything you create lives in /data: tenant profiles, test profiles, tokens, certificates, results
RUN useradd --create-home --uid 10001 tester && mkdir -p /data && chown tester:tester /data
USER tester
WORKDIR /data
VOLUME ["/data"]

EXPOSE 8089
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "/app/healthcheck.py"]
ENTRYPOINT ["locust", "-f", "/app/locustfile.py"]
CMD ["--web-host", "0.0.0.0", "--web-port", "8089"]
