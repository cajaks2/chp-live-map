FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app.py comments.py ecs_logging.py geo_bounds.py manage_comments.py scrape_chp_traffic.py generate_live_map.py serve_live_map.py /app/

EXPOSE 8080

CMD ["sh", "-c", "exec gunicorn app:app -k uvicorn.workers.UvicornWorker --workers ${WEB_WORKERS:-1} --bind ${HTTP_HOST:-0.0.0.0}:${HTTP_PORT:-8080} --access-logfile /dev/null --error-logfile -"]
