# TailBridge 运维

以下面向 Ubuntu 原生部署。本机环境操作与验证范围见 [E2E 说明](E2E_TEST_GUIDE.md)。

## 服务与证书

```bash
sudo systemctl status headscale --no-pager
sudo journalctl -u headscale -n 50 --no-pager
sudo -u headscale headscale configtest
curl -fsS https://tailbridge.example.net/health
```

修改前保留旧配置，以服务用户校验后再重载或重启。自动证书的端口与 DNS 要求见[安装说明](INSTALL.md#安全组与防火墙)；监测证书到期和签发失败，保留 ACME 缓存。手动证书更新后重启对应服务。

管理 API Key 的签发和轮换见 [REST 管理](API.md#认证)。UI 会话密钥单独轮换，不会自动撤销设备身份。

## 备份与升级

备份 `/etc/headscale/`、完整 `/var/lib/headscale/`、各中继身份和证书、可选 UI 状态，以及安装版本与额外服务配置。备份包含私钥，限制访问并保留异机副本。

停止 UI 写入及 Headscale 后复制状态，核验归档摘要，再恢复原运行状态。不要只复制运行中的 SQLite 主文件。升级前检查目标版本配置并跑 E2E；数据库已迁移时，回退必须恢复升级前完整备份及对应旧程序。[上游升级说明](https://headscale.net/stable/setup/upgrade/)

恢复先在隔离实例中核验用户、节点 ID / IP / 公钥、策略和真实连接，再切换入口。同一身份不要在两个正式实例同时运行；容器重启测试不替代恢复演练。

## 中继与访问策略

新增中继按 [命名规则](NODE_NAMING.md) 查重和分配 ID。先检查 DNS、TLS、STUN、接入校验，并通过隔离测试 Map 验证实际转发，再原子替换生产 `/etc/headscale/derp.yaml`，避免读到半写文件。Headscale 每分钟重读，成功后下发；客户端用 `tailscale debug derp-map` 确认，无需重启。部署 derper 不会自动注册到 Map。

重读由 `config.yaml` 中的 `derp.auto_update_enabled: true` 和 `derp.update_frequency: 1m` 控制。修改这两个设置须校验并重启 Headscale；定时重读只更新 Map 来源。[上游配置](https://github.com/juanfont/headscale/blob/v0.29.7/config-example.yaml#L130-L135)

退役时先发布不含旧节点的 Map，确认客户端切换，再停止旧中继。控制面维护会影响中继的新接入校验，应安排维护窗口。

访问策略以 `/etc/headscale/policy.hujson` 为准（`policy.mode: file`），REST API 不写策略。按用户、设备和目标端口授权，修改后执行 `sudo -u headscale headscale policy check --file /etc/headscale/policy.hujson`，通过后用 `sudo systemctl kill --signal=HUP --kill-who=main headscale` 重载，再验证允许与拒绝两种情况。节点注册、撤销和路由批准通过 [管理 API](API.md) 分别处理。

## 重启控制面

保留 `/etc/headscale/` 和 `/var/lib/headscale/`，执行 `sudo systemctl restart headscale`；不要通过重新安装或 `--force` 重启。节点恢复控制连接时沿用身份，无需重新注册。

- 注册、管理 API / UI 操作、策略及 Map 下发暂时不可用。
- 已建立的直连通常继续使用缓存密钥和路由；换网、重新发现或密钥到期可能影响连接，不保证业务零中断。[控制与数据平面](https://tailscale.com/docs/concepts/control-data-planes)
- 独立 derper 不随之停止，已有中继连接通常继续；当前设置 `verify-client-url-fail-open=false`，控制面不可达时中继的新接入或重连会被拒绝。[接入校验](https://github.com/juanfont/headscale/blob/v0.29.7/docs/ref/derp.md)

## 配置 Linux 出口

出口机器必须能访问目标网络。已接入的 Linux 节点执行：

```bash
curl -fsSL https://raw.githubusercontent.com/chinayin/tailbridge/main/install.sh | \
  sudo env SERVER_URL=https://tailbridge.example.net ADVERTISE_EXIT_NODE=true \
    sh -s -- --role node
```

首次安装还需注册密钥；参数及无 systemd 的处理见[安装说明](INSTALL.md#node-设备)。脚本写入 `/etc/sysctl.d/99-tailbridge-exit-node.conf` 并立即开启转发。IP 转发是 Linux 出口的必需项，不是性能优化；默认 netfilter 模式由 Tailscale 管理转发与 NAT 规则，无需手工全局放开 iptables。自定义防火墙仍需允许所需流量，脚本不会清空或改写其规则。[IP 转发要求](https://tailscale.com/docs/reference/troubleshooting/network-configuration/ip-forwarding-errors-advertise) · [netfilter 模式](https://tailscale.com/docs/reference/netfilter-modes)

通过 [REST 节点与路由接口](API.md#节点与路由)查询实际节点 ID，批准 `0.0.0.0/0` 和 `::/0`。

将下面的授权合并进现有 `grants`，把 `alice@` 替换为实际用户；它只授权互联网出口：

```json
{"src": ["alice@"], "dst": ["autogroup:internet"], "ip": ["*"]}
```

按[访问策略流程](#中继与访问策略)校验并重载。

路由批准与使用授权分别生效，客户端还须主动选择出口，见 [出口切换](#整机出口切换与排查)。[Headscale 路由](https://github.com/juanfont/headscale/blob/v0.29.7/docs/ref/routes.md#exit-node) · [Tailscale 出口](https://tailscale.com/docs/features/exit-nodes)

## 整机出口切换与排查

客户端默认不选择出口。在界面选择已授权出口，选择 None 恢复本地上网；CLI 示例：

```bash
tailscale set --exit-node=tb-node-hk-01 --exit-node-allow-lan-access=false
tailscale status
tailscale set --exit-node=
```

用实际出口名称替换示例。部分 Linux 安装需要 sudo；Mac 可使用界面。`--exit-node-allow-lan-access=true` 仅允许客户端访问当前局域网，不代表获准访问出口侧子网。

排查顺序：出口转发与路由发布 → 控制面批准 → `autogroup:internet` 授权 → 客户端选择 → 直连 / DERP 路径 → HTTP 源地址。分别检查 IPv4、IPv6、公共 DNS 和 MagicDNS。

`make e2e-exit` 验证模拟双栈出口、拒绝与故障恢复；执行前取消手动选择的出口。测试会恢复选择、LAN 偏好和服务；完整 `make e2e-test` 另测经 DERP 的出口流量。出口故障反例不等于系统级防泄漏保证，真实 Mac 换网与休眠仍待验。
