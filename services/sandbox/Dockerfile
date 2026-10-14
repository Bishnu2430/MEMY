FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN pip install fastapi "uvicorn[standard]" ruff bandit pytest
COPY server.py /srv/server.py
RUN useradd --create-home --uid 10002 sandbox
USER sandbox
WORKDIR /srv
EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
