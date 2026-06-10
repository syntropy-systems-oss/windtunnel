FROM python:3.12-slim

WORKDIR /app

# Install the MCP SDK with its CLI extras (FastMCP lives here)
RUN pip install --no-cache-dir "mcp[cli]>=1.0"

# Copy the dim's synthetic_db + server
COPY ../synthetic_db.py /app/synthetic_db.py
COPY server.py /app/server.py

# Failure mode is injected via env var at container run time:
#   docker run -e MOCK_MCP_FAILURE_MODE=malformed_json ...
# Valid values: malformed_json | timeout | empty_unexpected | (unset=normal)
ENV MOCK_MCP_FAILURE_MODE=""
ENV MOCK_MCP_TIMEOUT_SECONDS="10"

EXPOSE 8080

CMD ["python", "server.py"]
