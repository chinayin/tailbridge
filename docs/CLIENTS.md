# Mac / Linux 客户端

首次接入需要管理员授权，注册后客户端会保存服务器地址和设备身份。正常重启无需重新配置；退出登录、状态丢失或节点过期、被撤销时才需要重新认证。

## 接入

- **macOS 图形版**：安装并打开 [Tailscale 官方独立版](https://tailscale.com/docs/install/mac)（Homebrew 对应 `tailscale-app`），允许添加 VPN；在 **Settings → CLI integration → Show me how → Install Now** 启用命令行。[CLI 说明](https://tailscale.com/docs/reference/tailscale-cli?tab=macos)
- **macOS Homebrew 命令行版**：`brew install tailscale` 安装后，执行一次 `sudo brew services start tailscale` 启动服务并设为开机启动。[官方说明](https://github.com/tailscale/tailscale/wiki/Tailscaled-on-macOS#installing-tailscaled-from-homebrew)
- **Linux**：Ubuntu 26.04 / 26.10、Debian 13 可按[节点安装](INSTALL.md#安装与检查-2)完成安装和注册；其他发行版使用 [Tailscale 官方安装方式](https://tailscale.com/download/linux)。

尚未接入时，替换控制面 URL 后执行。本文的 `tailscale` 命令在 Mac 图形版无需 `sudo`；Homebrew 命令行版和 Linux 按权限加 `sudo`：

```bash
tailscale up --login-server=https://tailbridge.example.net
```

打开命令提示的注册链接，把页面上的 Auth ID 交给管理员，按 [REST 流程批准](API.md#批准客户端注册)并归属到你的用户后完成接入。页面上的 `headscale auth register` 供管理员使用，无需在客户端执行。设备默认沿用本机名称。

已有[单次节点注册密钥](API.md#用户与注册密钥)时，可在首次命令追加 `--auth-key='<节点注册密钥>'`，省去等待批准；`--auth-key=file:./node-auth.key` 只是从文件读取的可选写法。

若密钥已在变量中，可用 `--auth-key="$NODE_AUTHKEY"`。原生 CLI 不自动读取项目的 `SERVER_URL` / `NODE_AUTHKEY` 或 `.env`，仍需显式传参；注册后不再需要该密钥或 ENV。

## 日常使用

```bash
tailscale status
tailscale ip -4
tailscale exit-node list
```

通过 `status` 中的尾网 IP 或 MagicDNS 名称访问 SSH、NAS 等服务；访问仍需管理员授权及目标服务允许。

默认使用本地网络上网。选择已批准且有使用权限的出口后，整机互联网流量经该出口：

```bash
# Use an exit; replace with an available node name
tailscale set --exit-node=tb-node-jp-01 --exit-node-allow-lan-access=true

# Return to local Internet access
tailscale set --exit-node=
```

上述设置会保存，重启后仍生效。`true` 让当前局域网流量直接走本地网络；需要限制本地 LAN 访问时改为 `false`。这不授予出口侧子网访问权限。Mac 在 **Settings → Exit Nodes** 勾选 **Allow local network access**；通过菜单的 **Exit Node** 切换出口，选 **None** 取消。

临时断开或恢复尾网连接，不删除注册身份：

```bash
tailscale down
tailscale up
```

## 与 OpenVPN 同时使用

Tailscale 可与按网段分流的 OpenVPN 同时连接。使用出口时系统 DNS 由 Tailscale 接管（见[出口排查](OPERATIONS.md#整机出口切换与排查)），只在团队内网 DNS 解析到内网地址的域名会得到公网地址，表现为 403 或无法访问。

把这些域名写入本机 `/etc/hosts`，地址以内网 DNS 的结果为准；内网 DNS 地址见 `scutil --dns` 中 OpenVPN 接口下的 `nameserver`：

```bash
# Ask the internal DNS; replace the name and the DNS address
dig +short app.team.example @10.0.0.53
```

```text
10.0.1.20  app.team.example
```

```bash
dscacheutil -q host -a name app.team.example   # Expect the internal address
route -n get 10.0.1.20                          # Expect the OpenVPN interface
```

- 每个域名只写一行；内网 DNS 返回公网地址的域名不要写入。
- hosts 不依赖 DNS 优先级，与两个 VPN 的连接顺序无关；内网地址变更后须手动更新，OpenVPN 断开时这些域名不可用。
- OpenVPN 下发 `100.64.0.0/10` 路由时与尾网地址段重叠，不要同时连接。

已在 macOS + OpenVPN Connect 分流模式、出口开启时验证；Linux 用 `getent hosts` 和 `ip route get` 检查，未验证。

## 测试连接

取得本仓库的 [install.sh](../install.sh)，在文件所在目录执行；Homebrew 命令行版和 Linux 切换出口可能需要 `sudo`：

```bash
bash ./install.sh test
bash ./install.sh test --exit-node tb-node-jp-01
```

前者检查本机，后者临时切换本机出口并恢复原设置。依赖已有的 Tailscale、curl 和 Python 3，缺少工具提示跳过；验证范围为 IPv4。详细行为见[连通性测试](INSTALL.md#连通性测试)。
