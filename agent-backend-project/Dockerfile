# ============================================================
# 后端生产镜像（两阶段构建）
#
#   stage 1 builder：装编译工具链 + 把全部依赖装到 /install
#   stage 2 runtime：只把 /install 搬过来，编译器和 apt 缓存全都不进最终镜像
#
# 三个关键约束（改之前先看这里）：
#   1) 基础镜像**必须**是 Python 3.12：fastembed==0.2.7 声明 python<3.13，
#      3.13 装不上，整个意图识别链路会缺依赖。
#   2) **必须**装 gcc：crcmod 是 C 扩展且 PyPI 上没有任何 Linux wheel。
#      它是 cos-python-sdk-v5 的隐式依赖，漏了会导致 COS 上传/出图全挂。
#      builder 阶段装完就丢，最终镜像不会多出 300MB 的 build-essential。
#   3) cos-python-sdk-v5 的元数据把 requests / certifi 钉在上古版本
#      （requests<=2.27.1、certifi<=2021.10.8），与本项目的 langchain /
#      openai / httpx 链条直接死锁。所以本体用 --no-deps 装，真实依赖手工补齐。
#
# 国内网速：apt 与 pip 默认都换成清华源。要改成内网私服/buildkit 缓存，
# 用 --build-arg 覆盖 PIP_INDEX_URL 即可。
#
# 构建（在 agent-backend-project 目录下执行）：
#   docker build -t agent-backend:latest .
# ============================================================
ARG BASE_IMAGE=python:3.12-slim


# ------------------------------------------------------------
# stage 1：构建依赖
# ------------------------------------------------------------
FROM ${BASE_IMAGE} AS builder

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_TIMEOUT=120
ARG PIP_RETRIES=5

# apt 换清华源：兼容老式 sources.list 与新式 deb822 的 debian.sources。
# 两个文件哪个存在就改哪个，都不存在也不报错。
RUN set -eux; \
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
        if [ -f "$f" ]; then \
            sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' "$f"; \
            sed -i 's|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' "$f"; \
        fi; \
    done; \
    apt-get update; \
    apt-get install -y --no-install-recommends gcc; \
    rm -rf /var/lib/apt/lists/*

# pip 全局走国内源并放宽超时：容器内直连 files.pythonhosted.org 实测会 ReadTimeout
# （pip 默认只等 15 秒），加大超时和重试比反复重跑构建划算。
RUN pip config set global.index-url "$PIP_INDEX_URL" \
    && pip config set global.timeout "$PIP_TIMEOUT" \
    && pip config set global.retries "$PIP_RETRIES" \
    && pip config set global.disable-pip-version-check true

WORKDIR /build
COPY requirements.txt ./

# 一次装完所有依赖。**不做成多步 fallback**：requirements.txt 已经把所有版本钉死，
# 管线要么整体可解析、要么就是真冲突；静默降级只会让镜像里躺着一个缺依赖的服务。
# COS SDK 的真实依赖（xmltodict/six/crcmod/pycryptodome）显式列出，先装它们，
# 再 --no-deps 装本体，顺序反了会因为缺 crcmod 而导入失败。
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir xmltodict six crcmod pycryptodome \
    && pip install --no-cache-dir --no-deps cos-python-sdk-v5==1.9.44

# 构建期自检：依赖装错在构建时就炸，而不是等用户点开页面才发现。
# ⚠️ 这里**刻意不检查** `crcmod._crcfunext`（crcmod 的 C 加速扩展）：
#    crcmod 1.7 默认只装纯 Python 实现，要编译 C 版本必须显式
#    `python setup.py build_ext --crcmod`——pip 不会带这个参数，所以无论
#    有没有 gcc，装的都是纯 Python 版。开发机上同样是纯 Python 版且业务正常，
#    所以这不是缺陷，断言它只会让构建永远失败。
#    gcc 仍然保留（builder 阶段）：给其它只有 sdist 的包留编译能力，
#    也保证 crcmod 的 sdist 能正常构建（只是不会产出 C 扩展）。
#    如果哪天 COS 大文件上传的 CRC 计算成为瓶颈（crcmod 纯 Python 慢约 10 倍量级），
#    再单独加一步强制编译，而不是把它当成依赖安装的前提。
RUN python -c "import fastapi, sqlalchemy, alembic, pymysql; print('web/db deps OK')" \
    && python -c "import fastembed, onnxruntime, numpy; print('fastembed/onnx OK')" \
    && python -c "import langchain, langgraph, langchain_openai, openai; print('llm deps OK')" \
    && python -c "import qdrant_client, redis, dashscope, volcengine; print('infra deps OK')" \
    && python -c "import pypdfium2, docx, PIL; print('doc deps OK')" \
    && python -c "import qcloud_cos; print('COS sdk OK')" \
    && python -c "import crcmod; print('crcmod OK (pure python)')" \
    && python -c "import mcp; print('mcp sdk OK')"


# ------------------------------------------------------------
# stage 2：运行镜像
# ------------------------------------------------------------
FROM ${BASE_IMAGE} AS runtime

# 覆盖式升级基础镜像内的系统补丁（slim 镜像常滞后几周），顺手清缓存。
RUN set -eux; \
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
        if [ -f "$f" ]; then \
            sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' "$f"; \
            sed -i 's|security.debian.org|mirrors.tuna.tsinghua.edu.cn|g' "$f"; \
        fi; \
    done; \
    apt-get update; \
    apt-get upgrade -y; \
    apt-get install -y --no-install-recommends tini; \
    rm -rf /var/lib/apt/lists/*

# 只搬装好的依赖，不搬编译器。
COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # 容器内没有时区数据，显式钉住，避免日志与"下次复习时间"漂移。
    TZ=Asia/Shanghai \
    # 第三方 MCP server 走 stdio 子进程被拉起；它是我们自己的子进程，不是不可信输入。
    MCP_ALLOW_STDIO=1

WORKDIR /app

# 日志默认写"项目根/logs"，uploads 是 /uploads 静态挂载点——两者都在 compose 里挂卷，
# 这里先建好并放着不管，避免第一次没挂卷时因为目录不存在而启动失败。
RUN mkdir -p /app/logs /app/uploads

# 用通配符拷贝，让本目录新增 .py 文件无需改 Dockerfile，
# 具体的排除规则统一由 .dockerignore 负责。
COPY --chown=0:0 . /app

EXPOSE 8000

# tini 做 1 号进程：uvicorn 自己处理不了 SIGCHLD 回收，MCP 的 stdio 子进程
# 退出后会留僵尸进程。
ENTRYPOINT ["/usr/bin/tini", "--"]

# --workers 默认 1：应用里有进程内内存态（限流窗口、意图分类模型、MCP 连接），
# 多 worker 会让限流变成"每 worker 一份"，行为不一致。要扩就先扩副本数。
# --proxy-headers 让 uvicorn 采信 nginx 传来的 X-Forwarded-For/Proto，
# 这样限流按真实客户端 IP 计数、重定向也用对外协议。
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*", \
     "--no-access-log"]
