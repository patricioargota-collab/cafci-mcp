FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Railway inyecta PORT; el volumen persistente se monta en /data
ENV DB_PATH=/data/fci.db

CMD ["python", "-c", "import main; main.main()"]
