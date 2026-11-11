# 安装与配置

适用于 Ubuntu 26.04（`amd64` / `arm64`）。按机器承担的角色阅读：[control 控制面](#control-控制面)、[derp 中继](#derp-中继)、[node 设备](#node-设备)。同机部署多个角色时合并相应的端口要求。

首次只需确定控制入口和第一个中继，无需预先规划所有地域。可以先在一台机器安装 control + 独立 derper，后续再添加地域。控制面启动要求 Map 非空；节点接入前还需让其中的中继实际可达，见[架构](ARCHITECTURE.md#组件与角色)。

## 共用准备

在目标机通过远程安装器执行，参数放在 `sh -s --` 后，环境变量放在管道后的 `sudo env` 中。例如查看帮助：

```bash
curl -fsSL https://raw.githubusercontent.com/chinayin/tailbridge/main/install.sh | sh -s -- --help
```

自托管或固定版本时，将安装器下载地址和 control / derp 的 `INSTALL_BASE_URL` 指向同一份源码；该地址下须同时包含 `install.sh` 和 `files/`。

安装需要 root 权限，以写入系统目录、安装包和管理服务。示例中的 `sudo` 供有管理员权限的普通用户使用；已登录 root 时去掉 `sudo` 即可，`--dry-run` 不要求 root。Headscale 和 derper 分别使用服务账户 `headscale`、`derper`，与安装账户分开。

Ubuntu 支持两种传参方式：`env KEY=value command` 仅给该命令传变量；`export KEY=value` 给当前 shell 及后续子进程使用，均不自动持久化。`sudo env ...` 是先取得权限再设置变量；普通用户的 `export` 不保证能穿过 sudo，建议按示例显式传参。[env](https://manpages.ubuntu.com/manpages/noble/man1/env.1.html) · [sudo](https://manpages.ubuntu.com/manpages/noble/man8/sudo.8.html)

目标机需能解析 DNS，并访问安装目录、系统软件源和官方组件源。限制出站时，允许到所用 DNS 的 UDP / TCP 53，以及下载源的 TCP 443 / 80。控制和中继域名必须在入网前可解析，并位于尾网域之外。

云安全组和主机防火墙都需放行对应流量。下文“公网”指 IPv4 `0.0.0.0/0`；提供 IPv6 服务时另配 `::/0`。固定设备来源可限制为其公网 CIDR，漫游设备需允许变化的来源。管理 SSH 的 TCP 22 仅向管理员 IP / 堡垒机开放。

环境变量只作为安装输入，不自动读取 `.env`；修改 ENV 后需重新执行安装。模板使用同名 `${ENV_NAME}`，如 `server_url: "${SERVER_URL}"`；安装器只替换已知变量，发现未替换标记即失败。安装器与 `files/` 须一起更新。

## control 控制面

### 环境变量

| 变量 | 要求 | 默认值 | 用途与格式 |
| --- | --- | --- | --- |
| `INSTALL_BASE_URL` | 必填 | 无 | 模板根 URL，如 `https://raw.githubusercontent.com/chinayin/tailbridge/main`；从其 `/files/` 下载模板 |
| `SERVER_URL` | 必填 | 无（实际公网域需提供） | Headscale `server_url`：默认命名为 `https://tailbridge.<公网域>`，如 `https://tailbridge.example.net`；所有节点使用同一地址 |
| `BASE_DOMAIN` | 必填 | 无 | Headscale `dns.base_domain`：完整 MagicDNS 尾网域，如 `tb.example.net`，不含协议 |
| `ACME_EMAIL` | 自动证书必填 | 无 | Headscale `acme_email`：ACME 联系邮箱 |
| `TLS_HOSTNAME` | 自动证书必填 | 无 | Headscale `tls_letsencrypt_hostname`：控制面证书域名，不含协议、端口或路径 |
| `TLS_CERT_PATH` | 文件证书必填 | 无 | Headscale `tls_cert_path`：目标机 PEM 证书链的绝对路径，需提前放置 |
| `TLS_KEY_PATH` | 文件证书必填 | 无 | Headscale `tls_key_path`：目标机私钥的绝对路径，安装器授予服务组读取 |
| `DERP_REGION_ID` | 必填 | 无 | Map 键及两处 `regionid`；整数 `1–65535`，不带前导零，见[命名规则](NODE_NAMING.md#derp-地域) |
| `DERP_REGION_CODE` | 必填 | 无 | `regioncode`，如 `ap-east-1`；小写字母开头，可含数字、连字符，最多 32 字符 |
| `DERP_REGION_NAME` | 必填 | 无 | `regionname`，地域显示名，如 `Hong Kong` |
| `DERP_NODE_NAME` | 必填 | 无 | `nodes[].name`，如 `tb-derp-hk-01`；小写字母开头，可含数字、连字符，最多 48 字符 |
| `DERP_HOSTNAME` | 必填 | 无 | 中继服务域名，如 `tb-derp-hk-01.example.net`，见[服务域名](NODE_NAMING.md#服务域名) |
| `DERP_PORT` | 可选 | `443` | Map 中的中继 TLS 端口，整数 `1–65535`；与控制面共用 IP 时填 `8443`，省略或空值用默认值 |
| `DERP_IPV4` | 可选 | 空 | 中继可达 IPv4；留空时依赖 DNS |

证书模式二选一，另一模式的变量留空：自动证书使用 `ACME_EMAIL` / `TLS_HOSTNAME`；文件证书使用 `TLS_CERT_PATH` / `TLS_KEY_PATH`，续期由部署方负责，私有 CA 需客户端另行信任。

`DERP_*` 填写中继信息，由控制面将同一份 `files/derp.yaml` 渲染为 `/etc/headscale/derp.yaml`，向节点下发。独立 derper 不读取这个文件。首次安装初始化一个区域；后续多区域在控制面的同一文件中维护。

`SERVER_URL` 是本项目的安装变量，映射到 Headscale `server_url` 和 Tailscale `--login-server`。官方称后者为协调服务器的基础 URL；它不是设备地址或 DERP 地址。Tailscale 官方客户端默认使用 `https://controlplane.tailscale.com`，接入本项目时改用自己的入口。[官方定义](https://tailscale.com/docs/reference/tailscale-cli/up)

例如 `SERVER_URL=https://tailbridge.example.net` 对应 `TLS_HOSTNAME=tailbridge.example.net`。证书覆盖入口域名；脚本目前不自动推导或检查二者一致性。`BASE_DOMAIN=tb.example.net` 则用于生成 `设备名.tb.example.net`，不用于控制面证书。[Headscale 字段定义](https://github.com/juanfont/headscale/blob/v0.29.7/config-example.yaml)

### 安全组与防火墙

| 方向 | 协议 / 端口 | 来源或目标、用途 |
| --- | --- | --- |
| 入站 | TCP 443 | 节点和中继访问控制面；自动证书的 TLS-ALPN-01 验证须允许公网访问 |
| 出站 | TCP 443 | 自动证书模式访问 ACME 服务，完成签发和续期 |

控制面固定监听 TCP 443，自动证书使用 TLS-ALPN-01，不监听 TCP 80。DNS 须直指服务器；Cloudflare 设为 DNS only，避免代理终止验证所需的 TLS 握手。URL 显式写 `:9443` 不会改变监听端口，且自动验证仍要求公网 443 可达。TCP 9090（指标）和 50443（管理 gRPC）仅监听回环地址；纯控制面无需 UDP 3478。[TLS 说明](https://headscale.net/stable/ref/tls/)

### 安装与检查

以下使用自动证书，先替换地址、域名和邮箱：

```bash
curl -fsSL https://raw.githubusercontent.com/chinayin/tailbridge/main/install.sh | \
  sudo env INSTALL_BASE_URL=https://raw.githubusercontent.com/chinayin/tailbridge/main \
    SERVER_URL=https://tailbridge.example.net BASE_DOMAIN=tb.example.net \
    ACME_EMAIL=ops@example.net TLS_HOSTNAME=tailbridge.example.net \
    DERP_REGION_ID=910 DERP_REGION_CODE=ap-east-1 DERP_REGION_NAME='Hong Kong' \
    DERP_NODE_NAME=tb-derp-hk-01 \
    DERP_HOSTNAME=tb-derp-hk-01.example.net \
    sh -s -- --role control --dry-run
```

确认计划后去掉 `--dry-run` 执行。同机部署中继时增加 `DERP_PORT=8443`。文件证书模式将 `ACME_EMAIL` / `TLS_HOSTNAME` 替换为 `TLS_CERT_PATH=/etc/ssl/control/fullchain.pem` 和 `TLS_KEY_PATH=/etc/ssl/control/privkey.pem`。

```bash
sudo -u headscale headscale configtest
sudo systemctl status headscale --no-pager
curl -fsS https://tailbridge.example.net/health
```

配置检查可能创建数据库和密钥，因此使用服务用户。安装后的配置位于 `/etc/headscale/`：`config.yaml` 管理控制面、TLS、DNS 和地址池；`derp.yaml` 管理下发的中继列表；`policy.hujson` 管理授权，初始 `grants: []` 拒绝互访。

配置写入或校验失败时，安装器恢复原有三份配置及权限；首次安装则移除本次新建配置。用户、注册密钥、节点和路由通过 [REST API](API.md) 管理。

初始模板见 [files/](../files/)，固定地址池为 `100.77.0.0/23`、`fd7a:115c:a1e0:77::/64`，部署前检查冲突；内置和公共 DERP 均关闭。后续变更直接维护目标机配置，见[运维](OPERATIONS.md)。

## derp 中继

### 环境变量

| 变量 | 要求 | 默认值 | 用途与格式 |
| --- | --- | --- | --- |
| `INSTALL_BASE_URL` | 必填 | 无 | 与安装器同源的模板根 URL，从其 `/files/` 下载服务模板 |
| `SERVER_URL` | 必填 | 无 | 控制面的 HTTPS URL，用于请求 `/verify` 校验接入 |
| `DERP_HOSTNAME` | 必填 | 无 | 中继服务域名，与 control 的 Map 一致；不含协议、端口或路径 |
| `DERP_PORT` | 可选 | `443` | 中继实际 TLS 监听端口，整数 `1–65535`；与控制面共用 IP 时填 `8443` |
| `DERP_CERT_DIR` | 可选 | `/etc/derper/certs` | 已有 PEM 证书目录的绝对路径，文件名必须为 `<DERP_HOSTNAME>.crt` 和 `<DERP_HOSTNAME>.key` |

只使用文件证书，安装前准备包含中继域名 DNS SAN 的证书链和私钥。默认路径例如 `/etc/derper/certs/tb-derp-hk-01.example.net.crt` 和同名 `.key`；目录及父目录须允许 `derper` 遍历，可设为 `755` 或单独授权。安装器授予 `derper` 组读取证书和私钥，并检查该账户能读取两者。

### 安装与检查

```bash
curl -fsSL https://raw.githubusercontent.com/chinayin/tailbridge/main/install.sh | \
  sudo env INSTALL_BASE_URL=https://raw.githubusercontent.com/chinayin/tailbridge/main \
    SERVER_URL=https://tailbridge.example.net \
    DERP_HOSTNAME=tb-derp-hk-01.example.net \
    sh -s -- --role derp --dry-run
```

确认计划后去掉 `--dry-run` 执行。同机部署时增加 `DERP_PORT=8443`，control 的 Map 也须填写相同端口；修改 Map 不会更改中继监听。

安装器构建 `/usr/local/bin/derper`，将 [服务模板](../files/derper.service) 渲染为 `/etc/systemd/system/derper.service`，以 `derper` 用户和组运行。服务开机启动、失败自动重启；身份保存在 `/var/lib/derper/derper.key`，重跑和升级保留该文件，版本匹配时复用二进制。已有服务配置不同时须显式使用 `--force`。

服务关闭 HTTP 80、启用 UDP 3478 STUN，使用手动证书并向 `${SERVER_URL}/verify` 校验接入，固定 `-verify-client-url-fail-open=false`。无 systemd 时仍准备服务文件、账户和状态目录，由调用方按服务中的参数启动进程。

```bash
sudo systemctl status derper --no-pager
sudo journalctl -u derper -n 50 --no-pager
```

证书签发和续期由部署方负责，可使用外部工具通过 DNS-01 完成，无需开放 80。Headscale 的证书缓存不会自动供 derper 使用；续期后的权限和重启命令见[运维](OPERATIONS.md#服务与证书)。

### 安全组与防火墙

| 方向 | 协议 / 端口 | 来源或目标、用途 |
| --- | --- | --- |
| 入站 | TCP `DERP_PORT`：默认 443，同机 8443 | 公网节点的 DERP 连接；与启动参数 `-a` 一致 |
| 入站 | UDP 3478 | 公网节点的 STUN 探测 |
| 出站 | TCP 443 | 请求 `SERVER_URL/verify`；URL 有显式端口时使用该端口，如 `:9443` 对应 9443 |

独立中继开放 TCP 443、UDP 3478；与控制面同机时合计开放 TCP 443 / 8443、UDP 3478。中继域名须在入网前可解析，Map 保持 `canport80: false`。外部证书工具的出站要求另行配置。

地域 ID 和节点名称在控制面的 Map 中维护，见[节点命名](NODE_NAMING.md#derp-地域)。

### 与控制面同机

将 `tailbridge.example.net` 和 `tb-derp-hk-01.example.net` 解析到同一 IP。control 和 derp 两次安装均设置 `DERP_PORT=8443`，共用相同的 `SERVER_URL` 和 `DERP_HOSTNAME`。Headscale 监听 443，derper 监听 8443；不同域名不能让两个独立进程共享同一 IP 的 443。

## node 设备

### 环境变量

| 变量 | 要求 | 默认值 | 用途与格式 |
| --- | --- | --- | --- |
| `SERVER_URL` | 必填 | 无 | 与 control 配置的完整 URL 一致，作为 Tailscale `--login-server` |
| `NODE_AUTHKEY` | 首次注册必填 | 无 | 控制面签发的注册密钥；已有有效身份时忽略，不是管理 API Key，也不代表互访授权 |
| `NODE_HOSTNAME` | 可选 | 本机 hostname | 首次注册时指定设备名；重跑不改名 |
| `ADVERTISE_EXIT_NODE` | 可选 | `false` | `true` 持久化并开启 IPv4 / IPv6 转发，注册后宣告出口；省略、空值或 `false` 均保留已有设置，不关闭出口 |

节点无需填写 `BASE_DOMAIN`、证书变量或 region，DNS 和中继列表由控制面下发。

### 安全组与防火墙

| 方向 | 协议 / 端口 | 来源或目标、用途 |
| --- | --- | --- |
| 出站 | TCP 443 | 访问控制面及默认端口的中继；控制 URL 显式写端口时另放行该端口 |
| 出站 | TCP 8443，同机中继方案需要 | 对应控制面配置的 `DERP_PORT=8443`；节点从 Map 获取，无需重复设置 ENV |
| 出站 | UDP 3478 | 到中继的 STUN 探测 |
| 出站 | UDP 任意目标端口 | 节点间直连；本地源端口通常为 41641，对端经 NAT 后的端口可能变化 |
| 入站 | UDP 41641，可选 | 有助于公网直连；以实际 tailscaled 监听端口为准，入网和使用 DERP 不依赖此入站规则 |

无需将 SSH、NAS、远程桌面等业务端口直接暴露到公网；通过尾网访问时仍需 Headscale 策略与主机防火墙授权。出口节点另需允许转发及业务所需的外网出站，见[配置 Linux 出口](OPERATIONS.md#配置-linux-出口)。[Tailscale 防火墙说明](https://tailscale.com/docs/reference/faq/firewall-ports)

### 安装与检查

先按 [REST API 的用户与注册密钥流程](API.md#用户与注册密钥)签发单次使用的 key，再通过已有安全通道传给待安装节点。安装器不会自动向未注册设备下发密钥。

```bash
curl -fsSL https://raw.githubusercontent.com/chinayin/tailbridge/main/install.sh | \
  sudo env SERVER_URL=https://tailbridge.example.net \
    NODE_AUTHKEY='<首次注册密钥>' \
    sh -s -- --role node
sudo tailscale status
sudo tailscale netcheck
```

有 systemd 时安装器启动并注册；无 systemd 时由调用方启动、注册及宣告出口，安装器不消费 authkey，但仍按开关配置转发。已有有效身份重跑不需要密钥；损坏或失效状态保留并报错，须显式处理重新认证。手机和 Mac 使用官方客户端接入。[注册方式](https://headscale.net/stable/ref/registration/)

复用身份前会核对已保存的 `ControlURL`；与 `SERVER_URL` 不同、为空或无法读取时拒绝继续，`--force` 不切换身份。

需要出口时在上述命令中增加 `ADVERTISE_EXIT_NODE=true`，随后完成[控制面批准与授权](OPERATIONS.md#配置-linux-出口)。

## 可选管理界面

Headplane 不属于上述安装角色，控制地址、管理 API Key 和入口另行配置。管理端口仅开放给管理网或通过 SSH 隧道访问；当前 E2E 的 TCP 3000 只绑定宿主机回环地址，不需要公网安全组规则。

## 共用选项与验证

- `--dry-run`：显示计划，不下载、写入或启动；不验证 DNS、网络和证书有效性。
- `--force`：允许覆盖所选角色的不同配置；control 会重置已有策略和中继列表，derp 会覆盖服务配置，使用前先[备份](OPERATIONS.md#备份与升级)；不清除数据库、密钥或节点身份。
- `--headscale-version X.Y.Z`、`--tailscale-version X.Y.Z`：覆盖默认版本，后者同时用于 derper；不带 `v`。默认值见[脚本顶部](../install.sh)，Go 由维护者更新。
- `--version` 查看安装器版本，`--help` 查看帮助。退出码 `0` 表示所选阶段完成，`1` 表示参数或运行错误，`2` 表示前提不足；无 systemd 的成功不代表服务已启动。

`make test` / `make lint` 验证本地回归。本机网络测试与验证范围见 [E2E 使用说明](E2E_TEST_GUIDE.md)。
