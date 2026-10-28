FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY __init__.py server.py /app/upload_service/
ENV PORT=8080
CMD ["uvicorn", "upload_service.server:build_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
