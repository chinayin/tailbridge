# TailBridge 运维

以下面向原生主机部署，支持的平台见[安装与配置](INSTALL.md)。本机环境操作与验证范围见 [E2E 说明](E2E_TEST_GUIDE.md)。

## 服务与证书

日常巡检在控制面执行 `sudo tbctl status`，一次检查控制面、备份、全部中继和节点；检查项与阈值见 [tbctl 运维工具](INSTALL.md#tbctl-运维工具)。单项排查：

```bash
sudo systemctl status headscale --no-pager
sudo journalctl -u headscale -n 50 --no-pager
sudo systemctl status derper --no-pager
sudo journalctl -u derper -n 50 --no-pager
sudo -u headscale headscale configtest
curl -fsS https://tailbridge.example.net/health
```

修改前保留旧配置，以服务用户校验后再重载或重启；运行配置的位置和 DERP 修改方式见[配置持久化](INSTALL.md#配置持久化)。自动证书的端口与 DNS 要求见[安装说明](INSTALL.md#安全组与防火墙)；监测证书到期和签发失败，保留 ACME 缓存。手动证书更新后重启对应服务。

DERP 证书由外部工具续期。更新 `DERP_CERT_DIR` 中的同名 `.crt` / `.key` 后，恢复服务组读取权限并重启；以下使用[默认目录](INSTALL.md#derp-中继)，替换域名和证书来源：

```bash
sudo install -o root -g derper -m 644 /path/to/fullchain.pem /etc/derper/certs/tb-derp-hk-01.example.net.crt
sudo install -o root -g derper -m 640 /path/to/privkey.pem /etc/derper/certs/tb-derp-hk-01.example.net.key
sudo -u derper test -r /etc/derper/certs/tb-derp-hk-01.example.net.crt
sudo -u derper test -r /etc/derper/certs/tb-derp-hk-01.example.net.key
sudo systemctl restart derper
```

管理 API Key 的签发和轮换见 [REST 管理](API.md#认证)。UI 会话密钥单独轮换，不会自动撤销设备身份。

## 备份与升级

control 每日自动备份到本机 `/var/backups/headscale/`，逻辑与安全说明见 [tbctl 运维工具](INSTALL.md#tbctl-运维工具)。检查定时器和快照：

```bash
systemctl list-timers tbctl-backup.timer
sudo ls /var/backups/headscale/
```

本机备份只防误操作和升级失败，不防整机丢失；异机副本、TLS 私钥、各中继的 `/etc/derper/` 和 `/var/lib/derper/`、可选 UI 状态仍需另行备份。备份包含私钥，限制访问。

升级 Headscale 时，先执行一次 `sudo tbctl backup`，再用首次安装的相同 ENV 重跑 [control 安装命令](INSTALL.md#安装与检查)并追加 `--headscale-version X.Y.Z`；策略和 Map 保持不变，只有 `config.yaml` 与模板不同时才需要 `--force`，手改项的处理见[配置持久化](INSTALL.md#配置持久化)。升级前检查目标版本配置并跑 E2E。

数据库已迁移时，回退必须恢复升级前的快照及对应旧程序：停止 Headscale，删除 `db.sqlite-wal` / `db.sqlite-shm`，用快照替换 `db.sqlite` 并保持 `headscale` 所有者后再启动。[上游升级说明](https://headscale.net/stable/setup/upgrade/)

恢复先在隔离实例中核验用户、节点 ID / IP / 公钥、策略和真实连接，再切换入口。同一身份不要在两个正式实例同时运行；容器重启测试不替代恢复演练。

## 中继与访问策略

策略和 DERP Map 统一用 [`tbctl publish`](INSTALL.md#tbctl-运维工具) 发布。建议把两份文件的权威版本放在忽略的 `local/` 中做版本管理，回滚即重新发布旧版本。

新增中继按 [命名规则](NODE_NAMING.md) 查重和分配 ID。先检查 DNS、TLS、STUN、接入校验，再发布生产 Map，并用真实客户端在生产中验证经该区域的实际转发。Headscale 每分钟重读，成功后下发，客户端用 `tailscale debug derp-map` 确认，无需重启。部署 derper 不会自动注册到 Map。

重读由 `config.yaml` 中的 `derp.auto_update_enabled: true` 和 `derp.update_frequency: 1m` 控制。修改这两个设置须校验并重启 Headscale，对升级的影响见[配置持久化](INSTALL.md#配置持久化)；定时重读只更新 Map 来源。[上游配置](https://github.com/juanfont/headscale/blob/v0.29.7/config-example.yaml#L130-L135)

退役时先发布不含旧节点的 Map，确认客户端切换，再停止旧中继。控制面维护会影响中继的新接入校验，应安排维护窗口。

访问策略以控制面实际加载的 `/etc/headscale/policy.hujson` 为准（`policy.mode: file`），仓库模板只用于首次初始化；REST API 不写策略。按用户、设备和目标端口授权，用 `tbctl publish policy` 发布后，验证允许与拒绝两种情况。节点注册、撤销和路由批准通过 [管理 API](API.md) 分别处理。

## 重启控制面

保留 `/etc/headscale/` 和 `/var/lib/headscale/`，执行 `sudo systemctl restart headscale`；重启不需要重新安装。节点恢复控制连接时沿用身份，无需重新注册。

- 注册、管理 API / UI 操作、策略及 Map 下发暂时不可用。
- 已建立的直连通常继续使用缓存密钥和路由；换网、重新发现或密钥到期可能影响连接，不保证业务零中断。[控制与数据平面](https://tailscale.com/docs/concepts/control-data-planes)
- 独立 derper 不随之停止，已有中继连接通常继续；当前设置 `verify-client-url-fail-open=false`，控制面不可达时中继的新接入或重连会被拒绝。[接入校验](https://github.com/juanfont/headscale/blob/v0.29.7/docs/ref/derp.md)

## 配置 Linux 出口

出口机器必须能访问目标网络。已接入的 Linux 节点执行：

```bash
curl -fsSL https://raw.githubusercontent.com/chinayin/tailbridge/main/install.sh |
  sudo env SERVER_URL=https://tailbridge.example.net ADVERTISE_EXIT_NODE=true \
    bash -s -- --role node
```

首次安装还需注册密钥；参数及无 systemd 的处理见[安装说明](INSTALL.md#node-设备)。脚本写入 `/etc/sysctl.d/99-tailbridge-exit-node.conf` 并立即开启转发。IP 转发是 Linux 出口的必需项，不是性能优化；默认 netfilter 模式由 Tailscale 管理转发与 NAT 规则，无需手工全局放开 iptables。自定义防火墙仍需允许所需流量，脚本不会清空或改写其规则。[IP 转发要求](https://tailscale.com/docs/reference/troubleshooting/network-configuration/ip-forwarding-errors-advertise) · [netfilter 模式](https://tailscale.com/docs/reference/netfilter-modes)

通过 [REST 节点与路由接口](API.md#节点与路由)查询实际节点 ID，批准 `0.0.0.0/0` 和 `::/0`。

将下面的授权合并进现有 `grants`，把 `alice@` 替换为实际用户；它只授权互联网出口：

```json
{"src": ["alice@"], "dst": ["autogroup:internet"], "ip": ["*"]}
```

按[访问策略流程](#中继与访问策略)用 `tbctl publish policy` 发布。

路由批准与使用授权分别生效，客户端还须主动选择出口，见 [出口切换](#整机出口切换与排查)。[Headscale 路由](https://github.com/juanfont/headscale/blob/v0.29.7/docs/ref/routes.md#exit-node) · [Tailscale 出口](https://tailscale.com/docs/features/exit-nodes)

## 子网路由与内网域名

让尾网设备经某个节点访问它能到达的网段，如办公室内网或 OpenVPN 后的网段。以下步骤未在实际部署中验证。

在能访问目标网段的 Linux 节点上开启转发并宣告，网段替换为实际值；已是出口的节点无需重复开启转发：

```bash
printf '%s\n' 'net.ipv4.ip_forward=1' 'net.ipv6.conf.all.forwarding=1' |
  sudo tee /etc/sysctl.d/99-tailscale.conf
sudo sysctl -p /etc/sysctl.d/99-tailscale.conf
sudo tailscale set --advertise-routes=10.0.0.0/16
```

按[节点与路由](API.md#节点与路由)批准该网段，该接口替换完整批准列表。再把授权合并进 `grants` 并[发布](#中继与访问策略)：

```json
{"src": ["alice@"], "dst": ["10.0.0.0/16"], "ip": ["*"]}
```

Linux 客户端执行 `sudo tailscale set --accept-routes=true`，macOS 默认接收。用 `ip route get 10.0.0.1`（macOS 为 `route -n get`）确认路由进入 Tailscale 接口。

- 目标看到的来源是该节点的地址，回程无需另配路由。
- 宣告网段不要与接收端的本机网段重叠，如 Docker 默认的 `172.17.0.0/16`。

只在内网 DNS 解析的域名，在 `/etc/headscale/config.yaml` 指定该域的 DNS，DNS 地址须在已宣告的网段内；校验并重启后生效，对升级的影响见[配置持久化](INSTALL.md#配置持久化)：

```yaml
dns:
  nameservers:
    split:
      team.example: [10.0.0.53]
```

客户端自己连接 OpenVPN 时不需要子网路由，见[与 OpenVPN 同时使用](CLIENTS.md#与-openvpn-同时使用)。

## 整机出口切换与排查

出口选择、取消和局域网访问设置见[客户端指南](CLIENTS.md#日常使用)。Mac 上启停容器时出口短暂中断，见[与 OrbStack 同时使用](CLIENTS.md#与-orbstack-同时使用)。

排查顺序：出口转发与路由发布 → 控制面批准 → `autogroup:internet` 授权 → 客户端选择 → 直连 / DERP 路径 → HTTP 源地址。分别检查 IPv4、IPv6、公共 DNS 和 MagicDNS。

使用出口时，Tailscale 默认通过出口解析公共域名。DNS 故障先检查出口的解析能力及客户端是否启用 Tailscale DNS，MagicDNS 单独验证。[DNS 与出口](https://tailscale.com/docs/reference/dns-in-tailscale#nameservers-and-exit-nodes)

IPv6 故障先检查出口的公网 IPv6 地址、默认路由和云侧放行，再分别用 `curl -4`、`curl -6` 访问同一双栈目标。`--accept-routes=false` 只关闭子网路由接收，不会关闭出口默认路由。[参数定义](https://github.com/tailscale/tailscale/blob/v1.104.5/ipn/prefs.go)

自动验证见 [E2E 出口测试](E2E_TEST_GUIDE.md#http-出口测试拓扑)；测试覆盖与实际环境验收见[验证范围](E2E_TEST_GUIDE.md#报告与验证范围)。
