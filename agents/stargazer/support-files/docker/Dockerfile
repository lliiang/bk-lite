FROM python:3.12 AS community

ARG NEXUS_PYTHON_REPOSITY

RUN sed -i 's/deb.debian.org/repo.huaweicloud.com/g' /etc/apt/sources.list.d/debian.sources

RUN apt-get update && \
    apt-get install -y vim unzip curl pkg-config libssl-dev ipmitool && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Sanic 进程数：每进程独立 GIL，用于绕开单进程 CPU 天花板；可在运行时用 -e 覆盖
ENV SANIC_WORKERS=4
# 开发模式开关：true 启用 --debug，false 或不设置则关闭
ENV SANIC_DEBUG=false

WORKDIR /app

ADD . .

RUN if [ -n "$NEXUS_PYTHON_REPOSITY" ]; then \
    pip3 config set global.index-url "$NEXUS_PYTHON_REPOSITY" && \
    pip3 config set global.trusted-host "$(echo $NEXUS_PYTHON_REPOSITY | sed -E 's|^https?://([^/:]+).*|\1|')"; \
    fi

# 安装 uv
RUN pip3 install uv

# 使用 uv 安装依赖
RUN uv sync --all-groups --all-extras

CMD ["bash", "support-files/docker/start.sh"]

# 社区版使用 --target community；企业版保持默认的最后一个构建阶段。
FROM community AS enterprise

# Enterprise 通过 named context 注入，主 context 中的同名目录由 .dockerignore 排除。
COPY --from=enterprise_src . ./enterprise

ARG ENTERPRISE_SHA
RUN test -n "$ENTERPRISE_SHA" && printf '%s\n' "$ENTERPRISE_SHA" > /app/enterprise/.source-git-sha
LABEL com.weops.stargazer.enterprise.revision="$ENTERPRISE_SHA"

# 构建期即验证本次事故涉及的 Enterprise 插件入口，禁止坏镜像进入仓库。
RUN uv run python -c "import enterprise.plugins.inputs.sangforhci.sangforhci_info; import enterprise.plugins.inputs.sangforscp.sangforscp_info"
