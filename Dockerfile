FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY ecs_logging.py geo_bounds.py scrape_chp_traffic.py generate_live_map.py serve_live_map.py /app/

EXPOSE 8080

CMD ["python3", "/app/serve_live_map.py"]
