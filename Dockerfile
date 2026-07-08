FROM python:3.11-slim

RUN pip install --no-cache-dir fastapi uvicorn "httpx[http2]" playwright

RUN playwright install --with-deps chromium

RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH"
RUN playwright install chromium

WORKDIR /app
COPY --chown=user . .

EXPOSE 7860
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
