# check=skip=InvalidDefaultArgInFrom
# 只准备系统依赖与测试工具，不在构建期安装尾网组件。
# 基础镜像由 tests/e2e/e2e.py 传入，默认值只在 settings.py 维护
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       bash ca-certificates curl iproute2 iptables iputils-ping openssh-client openssh-server python3 \
    && passwd -d root \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
