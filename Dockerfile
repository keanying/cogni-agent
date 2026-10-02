# 启用 BuildKit 缓存挂载(--mount=type=cache)复用 npm/uv 下载缓存，层失效也不重复下载。
# 不写 # syntax 指令：Jenkins 的镜像加速器(阿里云 mirror)对 docker/dockerfile frontend
# 返回 403；现代 Docker(BuildKit 0.8+/Docker 23+)默认 frontend 已内置该语法，无需显式指定。

# ── 阶段 1: 前端构建 ──
FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/node:22 AS web-builder
WORKDIR /web
# npm 走华为云镜像加速；package-lock.json 为 lockfileVersion 3（平台无关），
# 其中的 darwin 原生包均为 optionalDependencies，linux 构建时 npm 自动跳过，
# 可安全拷贝并使用 npm ci 锁定依赖、确定性安装（比 npm install 快 2~5 倍）
RUN npm config set registry https://mirrors.huaweicloud.com/repository/npm/
COPY web/package.json web/package-lock.json ./
# --mount=type=cache：npm 下载缓存挂载在 /root/.npm，锁文件未变时即使层重建也只解压不重下
RUN --mount=type=cache,target=/root/.npm npm ci
# npm bug #4828: 可选原生平台依赖可能漏装，根据容器实际架构显式补装
RUN --mount=type=cache,target=/root/.npm \
    ARCH=$(uname -m | sed 's/x86_64/x64/;s/aarch64/arm64/') && \
    npm install --no-save @tailwindcss/oxide-linux-${ARCH}-gnu
COPY web/ ./
RUN npm run build

# ── 阶段 2: 后端运行镜像 ──
FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/python:3.12-slim
WORKDIR /app

# 关闭 stdout 缓冲,确保容器日志实时输出(避免 pod 被 K8s 杀掉时日志还在缓冲区)
ENV PYTHONUNBUFFERED=1

# ── 运行环境标记（config.py 据此加载 .env.{ZHILV_ENV} 文件）──
ENV ZHILV_ENV=dev

# ── 配置注入策略 ──
# 优先级：docker build --build-arg > .env.dev 文件 > 以下 ARG 默认值
# 非敏感值（host/port/db 名）提供默认值，方便本地 docker build 直接使用；
# 敏感值（密码/密钥）默认留空，由 CI（Jenkins）通过 --build-arg 注入，
# 或本地开发时由 .env.dev 文件通过 config.py 的 dotenv 加载。
ARG POSTGRES_HOST=10.53.0.2
ARG POSTGRES_PORT=5432
ARG POSTGRES_DB=zhilv
ARG POSTGRES_USER=postgres
ARG POSTGRES_PASSWORD
ARG REDIS_HOST=10.53.0.4
ARG REDIS_PORT=6379
ARG REDIS_PASSWORD
ARG REDIS_DB=2
ARG MINIO_ENDPOINT=localhost:9000
ARG MINIO_ACCESS_KEY=minioadmin
ARG MINIO_SECRET_KEY=minioadmin
ARG JWT_SECRET
ARG CREDENTIAL_FERNET_KEY
ARG SMS_ACCESS_KEY
ARG SMS_ACCESS_SECRET
ARG DEEPSEEK_API_KEY
ARG DASHSCOPE_API_KEY
ARG WEBSEARCH_API_KEY

ENV POSTGRES_HOST=$POSTGRES_HOST
ENV POSTGRES_PORT=$POSTGRES_PORT
ENV POSTGRES_DB=$POSTGRES_DB
ENV POSTGRES_USER=$POSTGRES_USER
ENV POSTGRES_PASSWORD=$POSTGRES_PASSWORD
ENV REDIS_HOST=$REDIS_HOST
ENV REDIS_PORT=$REDIS_PORT
ENV REDIS_PASSWORD=$REDIS_PASSWORD
ENV REDIS_DB=$REDIS_DB
ENV MINIO_ENDPOINT=$MINIO_ENDPOINT
ENV MINIO_ACCESS_KEY=$MINIO_ACCESS_KEY
ENV MINIO_SECRET_KEY=$MINIO_SECRET_KEY
ENV JWT_SECRET=$JWT_SECRET
ENV CREDENTIAL_FERNET_KEY=$CREDENTIAL_FERNET_KEY
ENV SMS_ACCESS_KEY=$SMS_ACCESS_KEY
ENV SMS_ACCESS_SECRET=$SMS_ACCESS_SECRET
ENV DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY
ENV DASHSCOPE_API_KEY=$DASHSCOPE_API_KEY
ENV WEBSEARCH_API_KEY=$WEBSEARCH_API_KEY

# 默认使用华为云 PyPI 镜像加速
ARG UV_INDEX_URL=https://mirrors.huaweicloud.com/repository/pypi/simple
# 可选：构建时传 --build-arg UV_SYNC_VERBOSE=-v 让 uv 逐包打印下载/安装明细日志（默认静默）
ARG UV_SYNC_VERBOSE=

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY pyproject.toml uv.lock* /app/
# --mount=type=cache：uv 下载缓存挂载在 /root/.cache/uv，锁文件未变时复用缓存快速命中
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project ${UV_SYNC_VERBOSE} --index-url ${UV_INDEX_URL} || \
    uv sync --no-dev --no-install-project ${UV_SYNC_VERBOSE} --index-url ${UV_INDEX_URL}

COPY . /app
COPY --from=web-builder /web/dist /app/web/dist

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev ${UV_SYNC_VERBOSE} --index-url ${UV_INDEX_URL} || \
    uv sync --no-dev ${UV_SYNC_VERBOSE} --index-url ${UV_INDEX_URL}

EXPOSE 8000
# uv run 默认在运行前触发 uv sync 且包含 dev 依赖组，会导致容器启动时重新下载
# ruff/black/pytest 等开发工具（生产网络到 pypi 慢时会卡死启动，8000 无监听）。
# 运行依赖已在构建阶段装好，启动加 --no-sync --no-dev 跳过同步，避免该问题。
CMD ["uv", "run", "--no-sync", "--no-dev", "uvicorn", "cogni_agent.main:app", "--host", "0.0.0.0", "--port", "8000"]
