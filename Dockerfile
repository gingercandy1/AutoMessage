# Dockerfile
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt



COPY app/ /app/app/
COPY migrations/ /app/migrations/
COPY alembic.ini /app/alembic.ini

# 注：.env 不 COPY 进镜像（避免密钥固化进镜像层）。
# 密钥由 docker-compose 的 env_file: .env 在运行时注入容器环境变量，
# pydantic-settings 从 os.environ 读取。

# 运行
CMD ["python", "-m", "app.bot.main"]