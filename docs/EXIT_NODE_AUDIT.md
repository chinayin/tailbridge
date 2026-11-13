# 出口节点审计与整改方案

本文保留原审计分析供复核；其中部分技术结论尚需更正，不应直接据此整改。域名、地址、设备名和用户已替换为示例值，不能直接用于连接或验证。原文声明采集过程只读。

- 采集端：macOS 客户端 `client-mac`（`100.64.0.2`，Tailscale 1.104.5）
- 出口端：`exit-node`（`100.64.0.1`，Ubuntu 26.04，Tailscale 1.104.5，AWS `ap-northeast-1`）
- 控制面：Headscale v0.29.7，`policy.mode: file`，策略文件 `/etc/headscale/policy.hujson`
- 结论有效期：配置项与实测值均为快照，整改后须按[第 6 节](#6-验证)重新采集

## 1. 实测拓扑

```text
macOS client 100.64.0.2
  └─ utun10  MTU 1280  default route 全部指向此处
      └─ WireGuard direct UDP 203.0.113.10:41641（未使用 DERP 中继）
          └─ exit-node 100.64.0.1 / AWS ap-northeast-1（东京）
              └─ 公网出口 203.0.113.10（AS16509）
```

| 项目 | 实测值 |
| --- | --- |
| 出口 IP | `203.0.113.10`，连续 5 次采样一致，东京 |
| 连接方式 | direct（58 ms）；备用 DERP `ap-northeast-2` 84.9 ms |
| `RouteAll` | true |
| `ExitNodeAllowLANAccess` | false |
| `CorpDNS` / MagicDNS | true / `tb.example.net` |
| `ShieldsUp` / `NoSNAT` | false / false |
| 通告路由 | `0.0.0.0/0`、`::/0` 已批准 |
| tailnet 规模 | 2 个节点（1 客户端 + 1 出口），单用户 |
| `serve` / `funnel` | 无配置 |

延迟与吞吐（经出口实测）：

| 指标 | 值 |
| --- | --- |
| RTT 1.1.1.1 / 8.8.8.8 | 61.9 ms / 65.2 ms |
| RTT www.baidu.com / www.taobao.com | 63.2 ms / 60.1 ms |
| 30 包丢包率 | 0% |
| 抖动 | stddev 31.5 ms，max 171.6 ms |
| 下行吞吐 | 约 1.95 MB/s（≈16 Mbps） |

默认路由已确认全部经 `utun10`，隧道内另含 `192.168.0/22`、`224.0.0.0/4`、`255.255.255.255/32`、`198.18.0.0/15` 等条目。

## 2. 结论摘要

加密与保密性没有发现问题：WireGuard 端到端加密、直连不经中继、DNS 未泄漏到国内解析器、无入站暴露。**问题集中在访问控制、数据出境范围、审计留存和单点可用性**。

| ID | 问题 | 级别 | 整改落点 |
| --- | --- | --- | --- |
| F-01 | 策略为空，等价 allow-all | 高 | `files/policy.hujson` |
| F-02 | 无分流，境内流量一并出境 | 高 | 策略 + 出口形态（见 [F-02](#f-02-无分流境内流量全部出境)） |
| F-03 | DNS 上游未显式配置，靠隐式回退 | 中 | `files/config.yaml` `dns` 段 |
| F-04 | 节点密钥无过期，注册密钥策略未固化 | 中 | 注册流程 / `docs/API.md` |
| F-05 | 出口侧无流量审计与留存 | 中 | 出口节点 / AWS 侧 |
| F-06 | 单出口、单点故障 | 中 | 新增第二出口 |
| F-07 | 局域网单播被切断（组播与 AirDrop 发现不受影响） | 中低 | 客户端设置 + 本文 [F-07](#f-07-局域网单播被切断组播正常) |
| F-08 | 客户端主机防火墙关闭 | 中低 | 客户端基线 |
| F-09 | 出口通告内网 endpoint `10.0.0.10:41641` | 低 | 云侧安全组 |
| F-10 | 出口 IPv6 缺省，`::/0` 指向隧道形成黑洞 | 中 | 出口节点 IPv6 |
| F-11 | 跨境链路吞吐受限 | 低 | 出口节点内核参数 |

### 与口头结论不同的三处更正

1. **组播没有被切断，路由表条目具有误导性。** `224.0.0.0/4` 与 `255.255.255.255/32` 确实指向 `utun10`，但 macOS 的组播发送按接口绑定（`IP_MULTICAST_IF`），不查单播路由表。实测 mDNS 收发完全正常，详见 [第 3 节](#3-exitnodeallowlanaccess-是什么)与 [F-07](#f-07-局域网单播被切断组播正常)。
2. **被切断的只有 en0 上的 IPv4 单播**，且此前用 `192.168.0.254` 作为探针是无效的——该地址不是网关也不存在。真实网关是 `192.168.0.1`，因为 `/32` 例外仍然可达。
3. **Tailnet Lock 不适用于本部署。** 该仓库使用 Headscale，不支持 Tailnet Lock。等价的控制手段是节点过期、注册密钥一次性、策略最小化，见 [F-04](#f-04-节点密钥与注册密钥治理)。

## 3. `ExitNodeAllowLANAccess` 是什么

Tailscale/Headscale 的一个客户端偏好，含义是：**使用出口节点时，本地局域网流量是否仍然从物理网卡直出。**

它只影响"本机到本地 LAN"，不影响出口侧的子网。相关变量是 `--exit-node-allow-lan-access`，默认 `false`。

### 两种取值的路由行为

选择出口后，客户端默认路由整体交给 `utun10`。`192.168.0/22` 这类本地网段会被显式加进隧道，避免"以为走了出口、实际局域网明文直出"的隐式泄漏。

| | `false`（当前） | `true` |
| --- | --- | --- |
| 互联网流量 | 经出口 | 经出口 |
| 本地 LAN（同网段其他主机） | 经出口，通常不可达 | 从物理网卡直出，可达 |
| 本地网关 `/32` | 例外，仍可达（DHCP/DNS 依赖） | 直出 |
| 局域网明文暴露给本地网络 | 否 | 是 |
| 打印机 / NAS / AirDrop / 投屏 | 不可用 | 可用 |
| 适用场景 | 强隔离、公共 Wi-Fi、合规要求"整机不触本地网" | 家庭/办公日常，需要局域网设备 |

### 本次实测证据（`false`）

测试台网络基线：`en0` = `192.168.0.10/22`，DHCP 网关 `192.168.0.1`。

```text
# 隧道接管了整段 LAN
netstat -rn | grep 192.168  → 192.168.0/22 → utun10（en0 上同名条目被屏蔽）
route -n get 192.168.0.20    → interface: utun10

# 单播：除自己与网关外全部不可达
ping 192.168.0.10    → 通（自身地址）
ping 192.168.0.1   → 4.0 ms 可达（/32 例外，见下）
ping 192.168.0.20    → 不可达（真实 LAN 主机）
ping 192.168.0.250     → 不可达
dig @192.168.0.1   → 正常应答（网关未被阻断）
dig @192.168.0.250     → connection timed out

# 组播：正常
route -n get 224.0.0.251 → interface: utun10（误导性条目）
再实际发 mDNS 查询 → 收到应答，来源 192.168.0.20
```

即：**互联网走了东京，LAN 单播被切断；只有自身地址与 DHCP 网关因为 `/32` 例外仍然通；组播不受影响。**

网关可达是 macOS + Tailscale 的刻意保留——否则 DHCP 续租、本地 DNS 会中断。这与 `docs/CLIENTS.md` 中"需要同时访问当前局域网时改为 `true`"的描述一致，属于预期行为而非缺陷；是否接受取决于使用场景，见 [F-07](#f-07-局域网单播被切断组播正常)。

### 组播与 AirDrop 的实际行为

这两者经常被混为一谈，实测结论是分开的：

| 层 | 路径 | 受出口影响 | 实测 |
| --- | --- | --- | --- |
| mDNS / Bonjour 发现 | `en0` 组播 `224.0.0.251` | 否 | 正常，见下 |
| AirDrop 发现与传输 | `awdl0` 链路本地 IPv6 `fe80::%awdl0` | 否 | 独立路由，不经 `utun10` |
| AirDrop BLE 唤醒 | 蓝牙 LE | 否 | 与 IP 路由无关 |
| LAN 内 NAS / 打印机 / SSH / 屏享 | `en0` IPv4 单播 | **是** | 不可达 |

组播实测证据：

```text
dns-sd -B _ipp._tcp local.    → 两台打印机（if=12 即 en0）
dns-sd -B _rfb._tcp local.    → 同网段 Mac（if=12）
自建 mDNS 查询 224.0.0.251    → 应答来源 192.168.0.20
```

结论：**组播层没有故障，不需要修复。** 出口节点切掉的是 `en0` 上的 IPv4 单播，因此受影响的是依赖直连 LAN 主机 IP 的功能（NAS、SMB、打印机数据通道、局域网 SSH、屏幕共享数据流），而 AirDrop 的发现与传输走 `awdl0` 与 BLE，理论上不受出口选择影响。

## 4. 逐条发现与整改方案

### F-01 策略为空，等价 allow-all

**证据**

```text
tailscale debug netmap | jq .PacketFilter   →  []
files/policy.hujson                          →  {"grants": []}
```

空 `PacketFilter` 表示不施加过滤，任意节点可访问任意节点的任意端口。出口节点因此可以主动连入客户端所有监听端口；客户端也能连入出口节点的 SSH 等管理面。

**整改**

在 `files/policy.hujson` 中建立白名单。约束：`grants` 非空后默认拒绝，须先写管理面再写业务面，否则会把自己锁在外面。

```json
{
  "groups": {
    "group:admin": ["alice@"],
    "group:exit-users": ["alice@"]
  },
  "tagOwners": {
    "tag:exit-node": ["group:admin"],
    "tag:server": ["group:admin"]
  },
  "grants": [
    { "src": ["group:admin"], "dst": ["tag:server"], "ip": ["22"] },
    { "src": ["group:exit-users"], "dst": ["autogroup:internet"], "ip": ["*"] },
    { "src": ["autogroup:member"], "dst": ["autogroup:self"], "ip": ["*"] }
  ]
}
```

要点：

- 出口只授 `autogroup:internet`，不要用 `"dst": ["*"]`。
- 管理端口（22）单独授权给 `group:admin`，其余一律不写。
- 节点打 `tag:exit-node` / `tag:server`，避免"按用户全通"。
- 按 `docs/OPERATIONS.md#中继与访问策略` 执行 `headscale policy check --file` → `systemctl kill -s HUP` 重载，并**同时验证允许与拒绝**两条路径。

**验收**：`PacketFilter` 非空；未授权端口连接被丢弃；已授权端口正常；Nebula 之外的节点互访失败。

### F-02 无分流，境内流量全部出境

**证据**

```text
traceroute www.baidu.com   → 1: 100.64.0.1  2: 242.13.44.x
dig www.baidu.com          → 98.98.225.192 / 164.52.120.52（海外节点）
dig www.taobao.com         → 155.102.209.237（海外入口）
RTT www.baidu.com          → 63.2 ms（本地直连应 <10 ms）
```

国内站点被解析到海外入口再回程，同时构成**跨境传输**：境内业务流量、含个人信息与凭证的 HTTPS 流量、DNS 查询全部经日本出口。

**整改：两条路线，按合规要求择一**

- **方案 A（保持全局出口，接受全量出境）**：适合纯个人出海场景。必须同时落实 [F-01](#f-01-策略为空等价-allow-all)、[F-03](#f-03-dns-上游未显式配置)、[F-05](#f-05-出口侧无审计与留存)，并在 `docs/OPERATIONS.md` 写明"出境内网流量范围"与出境评估依据。
- **方案 B（推荐，改为子网路由白名单）**：撤销 `0.0.0.0/0`、`::/0` 批准，出口节点改为广告**具体目标网段**，只把必须走东京的流量送出去，境内流量本地直出。

方案 B 操作形态：

```bash
# 出口节点侧：撤销全量广告，改为精确网段（按实际目标替换）
sudo tailscale set --advertise-exit-node=false --advertise-routes=10.20.0.0/16
# 控制面侧：批准新路由（替换完整列表，未列出的即撤销）
# POST /node/<id>/approve_routes  {"routes":["10.20.0.0/16"]}
```

注意 Headscale v0.29.7 不支持 Tailscale 的 app connector，因此**无法按域名分流**；域名级需求只能通过"广告目标网段"或"本地代理"实现。客户端侧的 `--exit-node-allow-lan-access` 不是分流开关，不能用于此目的。

**验收**：`traceroute` 到国内站点首跳为本地网关；到目标网段首跳为 `100.64.0.1`；`tailscale status` 不再显示 `exit node` 或仅显示受限路由。

### F-03 DNS 上游未显式配置

**证据**

```yaml
# files/config.yaml
dns:
  override_local_dns: false
  nameservers:
    global: []
```

当前无全局解析器，客户端回退到操作系统解析器。实测走隧道后解析落地在 AWS 东京（`198.51.100.53`），而直接查询本地网关则落到国内 ISP（`2001:db8::53`）：

```text
dig TXT whoami.ds.akahelp.net @100.100.100.100   → "ns" "198.51.100.53"        # 出口侧
dig TXT whoami.ds.akahelp.net @192.168.0.1     → "ns" "2001:db8::53"  # 国内 ISP
```

**当前没有泄漏，但状态是隐式的**：依赖"使用出口 + 系统解析器恰好不可达"的回退。一旦取消出口、切换节点，或 `override_local_dns` 变化，DNS 会静默回落国内 ISP，且不会有人察觉。

**整改**

```yaml
dns:
  magic_dns: true
  base_domain: "${BASE_DOMAIN}"
  override_local_dns: true      # 显式接管，不再依赖回退
  nameservers:
    global:
      - 1.1.1.1
      - 9.9.9.9
    split: {}
```

- 配合方案 B 时，`override_local_dns` 可保持 `false`，改用 `nameservers.split` 把需要走境内的域指向本地解析器，其余走全局。
- DNS 查询本身已在 WireGuard 隧道内，**不需要 DoH/DoT**，明文 DNS 已被加密包裹。
- 修改按 `docs/INSTALL.md#配置持久化` 校验后重启 Headscale，再在客户端 `scutil --dns` 确认。

**验收**：`scutil --dns` 显示预期解析器；`dig TXT whoami.ds.akahelp.net` 的落地与所选方案一致；取消出口后仍不回流国内 ISP。

### F-04 节点密钥与注册密钥治理

**证据**

```text
tailscale status --json → Self.KeyExpiry: None，Peer.KeyExpiry 缺省
节点未打 tag，身份为普通用户设备
```

节点密钥永不过期，设备丢失或镜像克隆后凭证长期有效。Headscale 无 Tailnet Lock，因此这里靠三件事替代：

1. **注册密钥一次性 + 短过期**：`--reusable=false --ephemeral=false --expiration 1h`，见 `docs/API.md#用户与注册密钥`。
2. **节点过期**：注册时给出过期时间，或后续 `POST /node/<id>/expire`，把 `expiry` 设为 90 天内；到期重新认证。
3. **管理 API key 轮换**：`docs/API.md#认证` 的流程照做，避免长期 key 常驻 `local/headscale-api.key`。

可选：`config.yaml` 里 `ephemeral_node_inactivity_timeout` 用于临时设备。

**验收**：`tailscale status` 中 `KeyExpiry` 有具体日期；`/api/v1/preauthkey` 中历史密钥均 `used: true` 或已过期。

### F-05 出口侧无审计与留存

Headscale 只记录控制面事件，**不记录数据面连接**。当前出口节点与 AWS 侧均无流量日志，出现问题无法溯源，也不满足"网络访问审计 + 留存"类要求。

**整改（按成本从低到高）**

1. AWS 侧开启 **VPC Flow Logs**（出口 EC2 所在 ENI）投递到 S3/CloudWatch，保留 ≥180 天。这是最省事且可审计的方案。
2. 出口节点上用 nftables 计数 + `conntrack` 采样，或定期 `ss -ti` 快照，落到本地轮转日志。
3. Headscale `log.level: info` 保留控制面事件；不要开到 `debug` 长期运行（含节点信息）。

声明边界：WireGuard 加密在出口节点终止，**出口节点是本链路上唯一的明文可见点**（DNS、SNI、非 TLS 流量）。任何日志方案都应按"处理个人信息"管理访问权限与保留期限。

**验收**：能看到按源 IP/目的 IP/端口的连接记录；保留策略与访问控制有文档记录。

### F-06 单出口、单点故障

只有一个出口节点，宕机即全网失去出口。同时出口是唯一信任点，被攻破等于全流量可见。

**整改**：新增第二个出口（不同可用区或不同区域，参考 `docs/NODE_NAMING.md` 命名），两个都 `ADVERTISE_EXIT_NODE=true` 并批准 `0.0.0.0/0`、`::/0`；`grants` 中用 `autogroup:internet` 同时覆盖两者。客户端切换与故障演练见 `docs/OPERATIONS.md#整机出口切换与排查` 和 `make e2e-exit`。

**验收**：停掉一个出口，客户端可切换到另一个并恢复出口；`make e2e-exit` 通过。

### F-07 局域网单播被切断，组播正常

见[第 3 节](#3-exitnodeallowlanaccess-是什么)。当前 `false` 的真实影响面：`en0` 上到 LAN 主机的 IPv4 单播不可达（实测 `192.168.0.20`、`192.168.0.250` 均不可达），只有自身地址与 DHCP 网关因为 `/32` 例外可达。

**不要修组播。** mDNS、Bonjour、AirDrop 发现链均未受影响（实测见[第 3 节](#组播与-airdrop-的实际行为)）：

- mDNS 收包正常，打印机与同网段 Mac 均能被 `dns-sd` 枚举
- AirDrop 的发现与传输走 `awdl0` 链路本地 IPv6 与 BLE，不经 `utun10`
- 自建 mDNS 探测有 `192.168.0.20` 应答，证明组播收发闭环正常

因此**不需要为“修复组播”改路由、加静态路由或改 Tailscale 组播设置**。这类改动既不生效（组播 socket 按接口绑定，不查单播路由表），又会增加维护负担。若 AirDrop 仍失败，按下面顺序排查，它们是 AirDrop 自身的条件而不是出口问题：

1. 双方均开启 Wi-Fi 与蓝牙，距离 9 米内；关闭飞行模式与低电量模式。
2. 接收权限不是“仅联系人”下的受限状态，试着切成“任何人 10 分钟”。
3. 系统设置 → 隐私与安全 → 本地网络，确保 Finder 有权限。
4. `ifconfig awdl0` 的 `ether` 长期为 `02:00:00:00:00:00` 表示 AWDL 空闲（未加入对等会话），不代表故障；开 AirDrop 面板后应变化。
5. 仍失败时临时 `tailscale set --exit-node=` 做对照：取消出口后恢复才能归因到出口配置；仍失败则与出口无关。

**真正需要恢复的是 LAN 单播**，这是取舍，不是缺陷：

| 场景 | 建议 |
| --- | --- |
| 家庭/办公主力机，需要 NAS / SMB / 打印机 / 屏享 | `--exit-node-allow-lan-access=true` |
| 公共 Wi-Fi、不受信网络 | 保持 `false`，接受局域网不可用 |
| 需要局域网但要求隔离 | 保持 `false`，通过 tailnet 内的节点访问 NAS，而不是走 LAN |

```bash
# 恢复局域网单播（可逆，随时改回 false）
tailscale set --exit-node-allow-lan-access=true
# 验证
tailscale status --json | jq '.ExitNodeStatus.ExitNodeAllowLANAccess'
ping -c 1 192.168.0.20 && nc -z -w 2 192.168.0.20 22
```

开启后的代价，即“本地网段直出、明文可见”：

- 本机到本地网段的流量回到 `en0` 广播域，同网段任何主机（访客设备、被入侵的 IoT 设备）都能看到这些包的源/目的与内容；未加密协议（SMBv1、Telnet、部分 NAS 管理面、HTTP）在本地链路上直接可读。
- 这就是“直出”：这部分流量不再经东京出口，本地网络运营方（公司 IT、民宿 / 咖啡馆 Wi-Fi）可对其镜像与审计。
- 公网侧不受影响，出网流量仍走出口。

须在 `docs/CLIENTS.md#日常使用` 补充这层代价说明，避免使用者误以为开 `true` 只是“多了个局域网”。

组播 `224.0.0.0/4`、广播 `255.255.255.255/32` 指向 `utun10` 只是 macOS 全量接管时产生的条目，实际不参与组播发送决策，也不需要为此改动路由。

**验收**：按选定策略，`ping` 与 NAS / 打印机 TCP 端口可达性符合预期；AirDrop 能在两台设备间完成一次实际文件传输（需人工，见第 8 节）。

### F-08 客户端主机防火墙关闭

**证据**：`socketfilterfw --getglobalstate` → `Firewall is disabled. (State = 0)`。

出口是单点信任，主机层不能是零防护。**整改**：开启应用防火墙，只放行需要的入站；这项属于客户端基线，建议写进 `docs/CLIENTS.md#接入` 的检查清单。

**验收**：`State = 1` 或 `2`，且已确认必要服务（SSH 等）仍可用。

### F-09 出口通告内网 endpoint

**证据**：节点 endpoint 同时通告 `203.0.113.10:41641` 与 `10.0.0.10:41641`。

内网 IP 作为候选 endpoint 会被下发到所有同 tailnet 客户端。风险低（该网段仅在 VPC 内可路由），但暴露了 VPC 地址规划。

**整改**（可选）：VPC 安全组限制 41641/UDP 来源；或用 SSM Session Manager 替代 SSH，减少出口节点入站面。不要为消除这条而关闭直连，直连是当前 58 ms 性能的前提。

**验收**：安全组规则有明确来源限制，且直连仍建立（`tailscale status` 显示 direct）。

### F-10 出口 IPv6 缺省

**证据**：`netcheck` → `IPv6: no`；客户端 `netstat -rn -f inet6` 默认路由指向 `utun10`，但本机 `en0` 无全球 IPv6 地址与 `fe80:` 之外地址。

当前无影响（无可路由的 v6 流量）。风险在于**一旦接入带 IPv6 的网络（家宽、移动热点），`::/0` 全部指向隧道，而出口无 v6，形成黑洞**，表现为应用超时后回退 v4，首包延迟变差。

**整改**：出口节点分配 IPv6（EC2 加 IPv6 或 NAT64）并把 `::/0` 批准；若不打算支持 v6，则客户端显式 `--accept-routes=false` 并把 `::/0` 从批准列表移除，避免黑洞。

**验收**：接入带 v6 的网络后，`curl -6` 结果与所选策略一致，无长时间挂起。

### F-11 跨境链路吞吐

约 16 Mbps、抖动 stddev 31.5 ms，是跨境 UDP 链路固有属性，不是配置错误。可做的两件事：

1. 出口节点开 BBR：`net.ipv4.tcp_congestion_control=bbr` + `net.core.default_qdisc=fq`，对跨境 TCP 吞吐通常有正向收益。`install.sh` 的出口分支（`ADVERTISE_EXIT_NODE=true`，当前只写 IP 转发）可一并考虑，但**不要在有争议时静默改内核参数**，需显式开关。
2. MTU 1280 是隧道封装后的保守值，不要手工调大，容易踩 PMTU 黑洞。

**验收**：调整前后同条件对比下载吞吐，记录到本文档或用例中。

## 5. 执行顺序

1. **先备份**：`/etc/headscale/`、`/var/lib/headscale/`、`local/` 下部署产物（见 `docs/OPERATIONS.md#备份与升级`）。
2. **定策略**：先回答 [F-07](#f-07-局域网单播被切断组播正常) 与 [F-02](#f-02-无分流境内流量全部出境) 的两个取舍，这两项决定后续所有配置。
3. **收紧控制面**：[F-04](#f-04-节点密钥与注册密钥治理) → [F-01](#f-01-策略为空等价-allow-all)（先管理面授权，避免锁死）。
4. **固化 DNS**：[F-03](#f-03-dns-上游未显式配置)。
5. **补审计**：[F-05](#f-05-出口侧无审计与留存) → [F-09](#f-09-出口通告内网-endpoint)。
6. **提可用性**：[F-06](#f-06-单出口单点故障) → [F-10](#f-10-出口-ipv6-缺省)。
7. **客户端基线**：[F-07](#f-07-局域网单播被切断组播正常) → [F-08](#f-08-客户端主机防火墙关闭)。
8. **最后调优**：[F-11](#f-11-跨境链路吞吐)。
9. 同步更新 `docs/OPERATIONS.md`、`docs/CLIENTS.md`，并在 `files/policy.hujson` / `files/config.yaml` 中体现最终态。

每一步都按 `docs/OPERATIONS.md` 的"先校验、再重载"流程做，不要用重装或 `--force` 重启。

## 6. 验证

整改前后各跑一遍，作为对比基线。

```bash
# 控制面
sudo -u headscale headscale policy check --file /etc/headscale/policy.hujson
tailscale debug netmap | jq '.PacketFilter | length'     # 期望 > 0
tailscale status --json | jq '.Self.KeyExpiry'           # 期望有日期

# 出口与路径
curl -s --max-time 8 https://ipinfo.io/json | jq -r '.ip, .city, .org'
tailscale status | grep -E 'exit node|direct|relay'
traceroute -n -m 3 www.baidu.com

# DNS 是否仍走出口侧
dig +short TXT whoami.ds.akahelp.net @100.100.100.100
scutil --dns | grep -E 'nameserver|search domain'

# 局域网单播（按 F-07 选定策略判读；192.168.0.254 不是网关，不要拿它当探针）
ipconfig getoption en0 router                            # 先确认真实网关
ping -c 1 192.168.0.1                                 # 网关：任何策略下都应可达
ping -c 1 192.168.0.20                                  # 普通 LAN 主机：false 时不可达、true 时可达

# 组播与发现（F-07 已确认无需修复，此处只做回归）
dns-sd -B _ipp._tcp local. | head -5                    # 应能看到打印机
dns-sd -B _rfb._tcp local. | head -5                    # 应能看到同网段 Mac

# 路由是否被拆分（F-02 方案 B）
netstat -rn -f inet | awk '$NF=="utun10"'

# 回归
make e2e-exit
```

`make e2e-exit` 会临时切换出口并恢复设置，执行前先取消手动选择的出口（见该用例说明）。出口故障反例不等于系统级防泄漏保证——真实 Mac 换网、休眠、断线回退仍需人工验证，这部分本次未覆盖。

## 7. 风险与回滚

| 动作 | 风险 | 回滚 |
| --- | --- | --- |
| `grants` 由空变非空 | 默认拒绝，可能锁死管理面 | 保留一份 `policy.hujson` 原件，`headscale policy check` 不过就不重载；锁死时用控制台恢复原文件再 HUP |
| 撤销 `0.0.0.0/0` 批准 | 客户端立即失去出口 | `approve_routes` 重新提交 `["0.0.0.0/0","::/0"]` |
| `override_local_dns: true` | 境内域名解析变慢或失败 | 改回 `false` 并重启 Headscale |
| 节点过期时间收紧 | 到期节点离线，需重新认证 | 提前准备注册密钥；不要对正在维护的节点设短期过期 |
| 开启 BBR | 与云厂商网络策略无冲突但属内核变更 | 删除对应 sysctl 文件并 `sysctl --system` |

## 8. 未覆盖范围

- 断线/换网时的防泄漏行为（路由层面观察到断线为黑洞、不会明文回落，但未做真实断网实测）
- macOS 休眠唤醒后的路由恢复
- **AirDrop 的真实对端传输未经实测**：本次只验证了 mDNS 组播收发闭环与 `awdl0` 状态，未在双机间实际传输文件。修复 agent 需人工补测一次发送与接收。
- 出口节点自身的补丁状态与 SSH 加固（仅确认 Ubuntu 26.04、无 `serve`/`funnel` 暴露）
- 出口节点的云侧 IAM、密钥与镜像来源
- 合规结论需结合组织的数据出境评估口径，本文只提供技术事实与范围界定

## 参考

- [出口配置](OPERATIONS.md#配置-linux-出口) · [中继与访问策略](OPERATIONS.md#中继与访问策略) · [出口切换与排查](OPERATIONS.md#整机出口切换与排查)
- [客户端日常使用](CLIENTS.md#日常使用) · [注册密钥](API.md#用户与注册密钥) · [节点与路由](API.md#节点与路由)
- [Headscale 路由与出口](https://github.com/juanfont/headscale/blob/v0.29.7/docs/ref/routes.md#exit-node)
- [Tailscale 出口节点](https://tailscale.com/docs/features/exit-nodes) · [控制与数据平面](https://tailscale.com/docs/concepts/control-data-planes) · [IP 转发要求](https://tailscale.com/docs/reference/troubleshooting/network-configuration/ip-forwarding-errors-advertise)
