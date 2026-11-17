# even-auth-gov 治理服务镜像。
# 构建:docker build -t even-auth-gov .
# 运行:见 deploy/casdoor/docker-compose.yml 的 even-auth-gov 服务(profile: gov)。
# 生产把基础镜像钉到 digest(python:3.12-slim@sha256:...),与 Casdoor 镜像同一原则。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY even_auth_gov/ even_auth_gov/

# 非 root 运行;/data 放审批状态库(审计留痕),由 compose 挂卷持久化。
RUN useradd --system --uid 10001 --home-dir /app gov \
    && mkdir -p /data && chown gov /data
USER gov
ENV APPROVAL_STORE_FILE=/data/approvals.json

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"

# 单实例、单 worker:飞书 WS 长连接 + 文件锁审批库都按单实例设计(见 deploy/casdoor/README「单实例约束」)。
CMD ["uvicorn", "--factory", "even_auth_gov.app:build_app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
