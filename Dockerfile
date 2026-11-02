FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY bundled bundled
COPY static static
COPY run.py .
EXPOSE 8000
CMD ["python", "run.py", "--host", "0.0.0.0", "--no-browser"]
