FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend

# 引入 uv（走 PyPI 安装，避免拉取 ghcr 镜像超时），用于快速、可缓存地装依赖。
RUN pip install --no-cache-dir uv

# 第一层：仅依赖 pyproject.toml，从中解析并安装第三方依赖（依赖清单的单一数据源）。
# 只要 pyproject.toml 不变，这一层命中缓存；改 backend 代码不会重新下载依赖。
COPY pyproject.toml .
RUN uv pip install --system --no-cache -r pyproject.toml

# 第二层：拷贝业务代码后仅安装本项目自身（--no-deps），不再重新拉取第三方依赖。
COPY backend ./backend
RUN uv pip install --system --no-cache --no-deps .

CMD ["python", "-m", "app.workers.feishu_ws"]
