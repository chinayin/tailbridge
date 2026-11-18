# 本机体验与 E2E

Compose 默认基于 Ubuntu 26.04 创建控制容器和三个独立节点容器；其中两个发起访问，`client-server` 提供 SSH 和按需出口。`probe` 返回普通 Docker 网络请求的来源 IP，`httpbin` 返回模拟外网请求的来源 IP。节点名称不代表真实操作系统或地理位置；这些设施不随服务器部署。

## 快速开始

需要 Docker Engine 和 Compose、Python 3、OpenSSL 和 `ssh-keygen`，并能访问官方包与 Go 模块下载源。在项目根目录执行：

```bash
make setup             # 工具依赖安装到 local/.venv
make e2e-init          # 创建本机配置，已有配置不覆盖
# 按需编辑 local/e2e/environment.yaml
make e2e-bootstrap     # 首次注册；已有身份使用 make e2e-up
make e2e-ui            # 可选管理界面
make e2e-basic         # 基础网络检查
```

WebUI 入口为 <http://127.0.0.1:3000/admin>，登录密钥位于 `local/e2e/generated/headplane-api-key`。全部命令见 `make help`。

## GitHub Actions

[CI](https://github.com/chinayin/tailbridge/actions/workflows/ci.yml) 在 main 推送和 PR 时运行 `make test`、`make lint`。[E2E](https://github.com/chinayin/tailbridge/actions/workflows/e2e.yml) 只手动触发：在 **Run workflow** 中选择基础镜像（`ubuntu:26.04`、`ubuntu:26.10` 或 `debian:13`），在 amd64 和 arm64 原生 runner 上分别创建全新测试环境，执行完整网络及三个角色的 systemd 验收。

测试环境不需要仓库密钥。E2E 报告按架构作为 `e2e-reports-amd64` / `e2e-reports-arm64` 保留 7 天，仅上传报告文件，不包含配置、证书或身份目录；结束时停止测试环境。

## 部署和测试的关系

`compose.yaml` 是完整静态编排。运行镜像只安装系统工具，由 `e2e.py` 经 stdin 传入 Dockerfile 构建，不发送构建上下文，`local/` 中的凭据不会到达 Docker daemon；`start.sh` 调用根目录 `install.sh` 安装组件。`installer-files` 提供安装器和 `files/` 模板，配置在目标容器替换并校验。本机 `e2e.py` 准备测试证书、SSH 凭据和可选 UI 配置。

容器直接运行 `headscale serve`、`derper`、`tailscaled` 和 SSH 服务，由 Compose 管理生命周期，不启动 systemd。DERP 入口先复制只读证书，再执行安装器，读取生成的 [DERP 配置](INSTALL.md#配置持久化)，并执行已安装 `derper.service` 中的同一 `ExecStart`，以 `derper` 用户启动。已有 control 容器重启直接复用配置；`make e2e-up` 先用安装器 `--dry-run` 校验输入，再删除测试的策略和 Map，重建 control 并按当前配置重新初始化。

E2E 从 `derp_map` 提取地域、节点名和端点，作为 `DERP_*` 环境变量传给 control，由安装器渲染共享模板。修改环境配置后执行 `make e2e-up`；单独 `docker restart` 不会更新环境变量。

Docker 的 `/proc/sys` 只读，出口的内核转发由 Compose `sysctls` 预先开启；安装器写入持久配置并确认内核值，`node.sh` 在注册后宣告出口。E2E 同时检查普通节点没有收到出口配置；容器测试不替代原生主机重启后的持久化验证。

容器基础镜像默认 `ubuntu:26.04`，兼容平台用 `TB_BASE_IMAGE` 验证，例如 `TB_BASE_IMAGE=debian:13` 或 `ubuntu:26.10`。基础检查会核对容器的发行版与版本和该变量一致。镜像标签按基础镜像区分，切换平台不会覆盖另一平台的镜像；状态卷按 Compose 项目区分，需要独立状态时使用独立实例：先把示例复制到 `local/e2e-debian/environment.yaml`，修改 `project`、`webui_port` 和出口子网（见[配置和状态](#配置和状态)），再运行：

```bash
TB_BASE_IMAGE=debian:13 TB_CONFIG=local/e2e-debian/environment.yaml make e2e-bootstrap e2e-test e2e-systemd
```

默认组件版本来自 [`install.sh`](../install.sh)。`e2e.py` 接收同名 `--headscale-version`、`--tailscale-version` 参数并传给容器中的安装器。control 在运行时校验并安装官方 DEB，node 从 Tailscale 官方 apt 源安装，derper 使用脚本内固定的 Go 版本编译。UI、探针及 HTTP 测试工具的镜像版本单独固定在 Compose 中。

首次启动依赖上游下载速度，控制面、节点和中继的安装就绪等待各有 30 分钟上限；普通 CLI 查询与节点注册仍使用短超时。安装进度可查看相应容器日志。

若官方源需要代理，可设置 `TB_INSTALL_PROXY` 为容器可访问的 HTTP(S) 代理 URL，再运行启动命令。入口将其作为安装器的 [`PROXY`](INSTALL.md#下载代理) 传入；本机模板直连，节点进程和 SSH、DERP、出口验证仍禁用代理。容器访问宿主机代理时应使用 `host.docker.internal`，不能使用容器自身的 `127.0.0.1`。

service 网络允许访问官方发布站点以完成安装，不发布控制面、中继或客户端的宿主端口；出口测试的 egress 链路仍为内部双栈网络。当前拓扑只测试一个区域，ID、代码、显示名和节点名均可配置。Compose 服务名 `derp-main` 只标识测试容器及其状态卷，与 Map 节点名无关。

初始策略拒绝设备互访。`make e2e-up` 每次重新初始化测试策略；SSH、DERP 就绪且拒绝策略到达节点后，先验证未授权 SSH 失败，再根据真实尾网地址放行测试互访，仅授权 `client-a` 使用互联网出口。测试策略通过 stdin 交给容器内与生产相同的 `tbctl publish policy`，校验、原子发布并重载，避免宿主机共享文件视图的同步延迟。`client-b` 为出口拒绝对照。首次注册使用固定测试用户 `e2e`，已有有效身份忽略 authkey；失效身份保留并报错，由操作层显式重新认证。注册凭据只在 `--bootstrap` 时临时挂载，随后移除。

## 配置和状态

环境配置位于 `local/e2e/environment.yaml`：

| 字段 | 含义 |
| --- | --- |
| `project` | Compose 项目名，默认 `tailbridge-e2e` |
| `base_domain` | 完整尾网域，示例 `e2e.tb.test`，无自动前缀 |
| `server_url` | 独立控制入口，示例 `https://hs.private.test`，仅在隔离 Docker 网络解析 |
| `derp_map` | 原生单区域映射，示例为 `910 / ap-east-1 / tb-derp-hk-01`；测试省略 ipv4，通过 Docker DNS 寻址 |
| `webui_port` | 本机回环地址上的 UI 端口 |
| `nodes` | 三个固定模拟服务的容器 hostname；Tailscale 注册直接沿用本机名 |
| `aliases` | `client-a`、`client-b`、`target`、`exit` 场景角色；`exit` 和 `target` 指向 `client-server`，别名本身不产生 DNS 或授权 |
| `exit_probe` | HTTP 目标及出口侧链路的 IPv4 / IPv6 子网和地址，均须显式配置 |

默认运行目录为 `local/e2e/`。使用 `--config local/<实例>/environment.yaml` 可指定另一实例；实际配置所在目录保存 `bundle/` 和 `generated/`。Compose 按项目管理卷；不同实例使用不同项目名、不重叠的出口侧链路子网及不冲突的 UI 端口。

状态卷、证书和 SSH 密钥应一起维护，不能只删除信任材料。测试证书有效期 30 天，API Key 初始有效期 7 天。

## 检查与停止

```bash
make e2e-up            # 按配置重建控制面，保留身份
make e2e-exit          # 双栈出口、拒绝对照与出口暂停/恢复
make e2e-test          # 完整检查：会短暂中断测试连接
make e2e-systemd       # 三种角色的真实 systemd 服务验收
make e2e-status
make e2e-down          # 保留卷、配置与凭据
```

完整检查包括 IP / 完整域名 / 短名 SSH、直连、强制 DERP、停止中继的失败反例、恢复、从运行 / 停用状态重启后的身份和已保存名称保持，以及停止 UI 后核心通信。另用 `tbctl publish derp` 临时修改地域显示名，验证定时下发、控制面重启保留配置与身份，最后恢复原 Map。出口检查覆盖 IPv4 / IPv6 HTTP、无权限反例、出口暂停恢复和经 DERP 转发。停止中继期间控制面继续运行，故障清理失败会报错。首次安装与默认拒绝证据单独写入 `generated/install-results.json`。

`make e2e-basic` 与 `make e2e-test` 还验证 [REST 管理](API.md)：使用临时 API Key、用户、注册密钥和独立 userspace 节点，测试真实注册、改名、到期、双栈路由批准／撤销与删除。测试完成或失败时清理临时资源并撤销 API Key，核对既有节点与路由不变；不使用或轮换 UI 的 API Key。

基础检查也会临时切换客户端出口，但不停止服务；出口和完整检查会短暂停止模拟出口。使用出口期间开启 LAN 访问，并验证本地 HTTP 探针仍经客户端本地地址直连。测试完成或失败时撤销临时选择并恢复 LAN 偏好，客户端平时保持不选出口。已有手动出口选择会触发前置检查失败，避免覆盖用户设置。

`make e2e-systemd` 在临时特权容器中以 systemd 为 PID 1，分别验收独立 DERP 和[三个角色同机](INSTALL.md#单机部署)：

- DERP：非 root 运行、TLS / STUN、配置与证书保护、代理下载、仅修改 conf 后重启生效、异常恢复和容器重启自启。使用已运行 E2E 中继的程序，不复制身份；此容器无外部网络。
- control / derp / node 同机：首次安装官方包及服务文件，中继复用已运行 E2E 中继的程序并监听 8443，node 从官方 apt 源安装并用 `NODE_AUTHKEY_FILE` 通过本地可信 TLS 完成真实注册，经控制面校验后接入本机中继；验证备份定时器生成快照、重跑幂等、发布策略后重跑 control 不覆盖策略、控制入口校验、出口开关及重启持久化、其他子网路由保留、停用与异常恢复、容器重启自启和身份保持。容器可出站下载官方包，通过 `TB_INSTALL_PROXY` 支持代理。

两项均不共享状态卷或发布宿主端口，结束后删除测试容器及其临时身份。只测 DERP 可运行 `make e2e-derp-service`；其他实例使用 `TB_CONFIG=local/<实例>/environment.yaml make e2e-systemd`。这验证容器中的服务生命周期，真实主机重启与公网链路仍须另行验收；完整转发用上述网络测试。

## HTTP 出口测试拓扑

`go-httpbin` 使用 Compose 中固定的镜像，`/ip` 返回 `origin`。请求禁用 curlrc 和代理，不设置 `X-Forwarded-For` 等来源头，直接读取 TCP 对端地址。

```text
client-a / client-b ── service 网络 ── client-server（exit）
                                          │
                                    egress 隔离双栈链路
                                          │
                                  httpbin-network 下一跳
                                          │
                                  lo 上的 HTTP 目标地址
                                          │
                              go-httpbin（共享网络命名空间）
```

`httpbin-network` 复用客户端 Ubuntu 镜像中的 `ip` 工具设置回环地址，不运行 Tailscale、不注册节点，也不持有状态卷。HTTP 服务使用自己的 go-httpbin 镜像；两个服务均不发布宿主端口。出口通过普通系统路由将目标网段指向该下一跳，尾网仍只发布默认路由，客户端不接受子网路由。

必须将 HTTP 目标放在出口的非直连网段：Tailscale 1.104.5 会从默认出口可达范围中剔除本地接口网段。直接将 HTTP 目标放在出口的直连 LAN 会测成局域网访问，而不是模拟互联网出口。[对应源码](https://github.com/tailscale/tailscale/blob/v1.104.5/ipn/ipnlocal/local.go#L3231)

断言同时核验：未选出口时目标不可达；授权客户端选择后系统默认路由进入 `tailscale0`；IPv4 / IPv6 回显地址等于出口的 egress 地址；MagicDNS SSH 正常；关闭出口后不可达；未授权客户端不能使用出口。完整测试还在阻断直连 UDP 后检查经自建 DERP 的 HTTP 回显。

## 报告与验证范围

本机 E2E 使用隔离模拟网络；systemd 专项覆盖三种角色的容器服务。真实主机重启、Mac 换网与休眠、公开 TLS / DNS、云安全组及跨网络链路未覆盖；出口故障测试不代表系统级防泄漏保证。证书与凭据轮换、家庭 / 团队子网路由、备份恢复须在实际环境另行验收。Mac 局域网服务和 AirDrop 文件传输也须实际操作验证。

测试结束后自动写入 HTML 和 JSON 报告，记录执行时间、配置、版本、通过的检查项和失败原因；同类报告保留最近一次结果：

- 完整测试：`local/e2e/generated/results.html`、`results.json`。
- 基础和出口专项运行后分别生成同目录的 `basic-results.*`、`exit-results.*`。

报告位于 Git 忽略的 `local/`，不提交。HTML 可直接用浏览器打开，查看检查证据、版本、运行配置及失败原因。

`make test` 运行安装器与 `tests/e2e/unit/` 中的离线回归。E2E 的 `e2e.py` 管理环境，`verify.py` 验证网络，`settings.py` / `config.py` 解析和校验输入，`report.py` 生成报告；Compose 和 shell 脚本负责容器启动。测试依赖由 `make setup` 装入 `local/.venv`。

`local/.venv` 是测试工具环境，`local/e2e/environment.yaml`、`bundle/`、`generated/` 保存当前配置、证书和凭据；Git 忽略不代表可以删除。历史日志和报告可清理，测试临时文件使用系统临时目录并自动删除。

## 手动操作

启动器的 `compose` 子命令只是传递相同配置和覆盖文件，可以使用标准 Compose 操作：

```bash
local/.venv/bin/python tests/e2e/e2e.py compose logs --tail 50 control
local/.venv/bin/python tests/e2e/e2e.py compose exec client-macbook \
  ssh -F /dev/null -i /run/secrets/ssh_client_key \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/etc/tailbridge/ssh/known_hosts \
  -o HostKeyAlias=target.tb root@client-server
```

`target.tb` 仅是 SSH 主机信任标识。`root` 仅用于隔离容器测试；真实设备的账号、公钥和主机信任由操作系统管理。

实际服务器操作见 [安装与配置](INSTALL.md)。
