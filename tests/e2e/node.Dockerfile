# 只准备系统依赖与测试工具，不在构建期安装尾网组件。
FROM ubuntu:26.04
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       bash ca-certificates curl iproute2 iptables iputils-ping openssh-client openssh-server python3 \
    && passwd -d root \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
