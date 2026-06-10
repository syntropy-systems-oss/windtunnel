FROM python:3.12-slim

WORKDIR /app

# Install the MCP SDK with its CLI extras (FastMCP lives here)
RUN pip install --no-cache-dir "mcp[cli]>=1.0"

# Copy the dim's synthetic_db + server
COPY ../synthetic_db.py /app/synthetic_db.py
COPY server.py /app/server.py

EXPOSE 8084

CMD ["python", "server.py"]
