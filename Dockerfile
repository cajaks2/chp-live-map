FROM python:3.12-slim

WORKDIR /app

COPY scrape_chp_traffic.py generate_live_map.py run_live_map.sh /app/

RUN chmod +x /app/run_live_map.sh

EXPOSE 8080

CMD ["/app/run_live_map.sh"]
