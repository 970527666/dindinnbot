FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN pip install --no-cache-dir flask supervisor
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY health.py .
CMD ["supervisord", "-n"]
