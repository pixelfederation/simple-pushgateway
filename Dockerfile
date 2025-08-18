# Use official Python 3.12 slim image
FROM python:3.13.7-slim

# Set working directory
WORKDIR /app

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY app ./app

# Set default port
ENV APP_PORT=8080
EXPOSE $APP_PORT

# Run the app
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port $APP_PORT"]
