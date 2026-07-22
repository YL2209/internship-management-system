# ---- 构建阶段 (Alpine) ----
FROM python:3.12.9-alpine AS builder

RUN apk add --no-cache \
        gcc musl-dev python3-dev binutils

WORKDIR /app

# 1. 隔离运行时依赖
COPY web/requirements.txt .
RUN pip install --target=/install --no-cache-dir -r requirements.txt

# 构建工具仅装在全局
RUN pip install --no-cache-dir cython setuptools jsmin htmlmin csscompressor

# 2. 复制源码并 Cython 编译
COPY core/ ./core_src/
COPY web/ ./web_src/

RUN <<'CYEOF' python -
import os, subprocess
src = '/app/core_src'
for root, dirs, files in os.walk(src):
    for f in files:
        if f.endswith('.py') and f != '__init__.py':
            fp = os.path.join(root, f)
            subprocess.check_call(['cythonize', '-i', '-3', fp], cwd=root)
print('core Cython build OK')
CYEOF

RUN <<'CYEOF' python -
import os, subprocess
src = '/app/web_src'
for root, dirs, files in os.walk(src):
    for f in files:
        if f.endswith('.py') and f != '__init__.py' and f != 'compress_assets.py':
            fp = os.path.join(root, f)
            subprocess.check_call(['cythonize', '-i', '-3', fp], cwd=root)
print('web Cython build OK')
CYEOF

# 3. 清理源文件
RUN find /app/core_src -name '*.py' ! -name '__init__.py' -delete \
    && find /app/core_src -name '*.c' -delete \
    && find /app/web_src -name '*.py' ! -name '__init__.py' ! -path '*/templates/*' ! -path '*/static/*' -delete \
    && find /app/web_src -name '*.c' -delete

# 4. 压缩静态资源
COPY web/compress_assets.py /app/
RUN python /app/compress_assets.py /app/web_src \
    && rm /app/compress_assets.py

# 5. 精简字体
RUN find /app/web_src/static/icons/fontawesome-free-*/webfonts -type f \
    ! -name 'fa-solid-900.woff2' \
    ! -name 'fa-regular-400.woff2' \
    ! -name 'fa-brands-400.woff2' \
    ! -name 'fa-v4compatibility.woff2' \
    -delete 2>/dev/null || true

# 6. .so 瘦身
RUN find /app -name "*.so" -exec strip --strip-unneeded {} \;

# ---- 运行阶段 (Alpine) ----
FROM python:3.12.9-alpine

LABEL org.opencontainers.image.title="sign-in-docker"
LABEL org.opencontainers.image.description="实习数据管理系统 (Web 管理后台 + 定时签到守护)"
LABEL org.opencontainers.image.version="2.1"

RUN apk add --no-cache tzdata ca-certificates \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone \
    && find /usr/share/zoneinfo -type f ! -path "*/Asia/Shanghai" ! -name "UTC" -delete

WORKDIR /app

COPY --from=builder /install /usr/local/lib/python3.12/site-packages
COPY --from=builder /install/bin /usr/local/bin
COPY --from=builder /app/core_src /app/core
COPY --from=builder /app/web_src /app/web

RUN find /usr/local/lib/python3.12 -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -name "*.pyc" -delete 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -type d -name "docs" -exec rm -rf {} + 2>/dev/null || true \
    && find /usr/local/lib/python3.12 -type f \( -name "README*" -o -name "LICENSE*" -o -name "CHANGELOG*" \) -delete 2>/dev/null || true

COPY run.py ./
COPY config.default.json .

RUN mkdir -p /app/logs /app/backups /app/journals /app/cache

ENV PYTHONPATH=/app \
    TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=5000 \
    CONFIG_PATH=/app/config.json \
    LOG_DIR=/app/logs \
    CACHE_DIR=/app/cache \
    DAEMON_ENABLED=true

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/api/health')" || exit 1

CMD ["waitress-serve", "--port=5000", "--threads=8", "web.app:app"]