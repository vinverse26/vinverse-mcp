FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py deploy_tool.py .

EXPOSE 8001

CMD ["python", "server.py"]
