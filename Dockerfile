FROM python:3.12-slim
WORKDIR /app
COPY server.py .
RUN mkdir -p /app/queue
EXPOSE 8787
ENV PORT=8787 QUEUE_DIR=/app/queue
CMD ["python3", "server.py"]
