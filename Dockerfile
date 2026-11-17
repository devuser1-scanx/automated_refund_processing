FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY automated_refund_processing/ automated_refund_processing/
CMD ["python", "-m", "automated_refund_processing.worker"]
