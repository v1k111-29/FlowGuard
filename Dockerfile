FROM python:3.11-slim

WORKDIR /app

COPY flowguard/requirements.txt ./flowguard/requirements.txt
RUN pip install --no-cache-dir -r flowguard/requirements.txt

COPY . .

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn flowguard.main:app --host 0.0.0.0 --port ${PORT}"]
