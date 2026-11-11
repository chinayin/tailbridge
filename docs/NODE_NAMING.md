# 节点命名

普通电脑直接沿用本机 hostname 注册，无需改名或配置 region。`regionid` / `regioncode` 只用于 DERP 中继，与普通设备所在位置无关。

## 机器名

管理用服务器采用 `tb-<角色>-<地域>-<序号>`，例如 `tb-derp-hk-01`；不加环境和云厂商前缀。

- 角色使用 `control`、`derp`、`node`；地域用 `us`、`jp`、`hr`、`hk`、`cn` 等约定的两字母短码。
- 家庭用 `home`，工作地点或工区用 `work`，无需加国内或海外标识。默认各一个站点，可省略序号，如 `tb-derp-home`、`tb-derp-work`。
- 云服务器序号从 `01` 开始，同角色、同短码跨云厂商统一编号。
- 使用小写字母、数字和连字符，首尾不用连字符。机器名最多 48 字符、DERP 区域代码最多 32 字符。

AWS `Name`、阿里云实例显示名、系统 hostname 和 Tailscale 设备名推荐一致，但需分别设置；云平台显示名不会自动修改本机名。专用中继的 `nodes[].name` 可沿用机器名。[AWS 标签说明](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/Using_Tags.html)

## 服务域名

控制入口默认命名为 `tailbridge.<公网域>`，例如 `tailbridge.example.net`；独立中继采用 `<DERP_NODE_NAME>.<公网域>`，例如 `tb-derp-hk-01.example.net`。这是项目命名约定，实际地址通过 `SERVER_URL` 填写，允许自定义；TLS 证书须与域名匹配。

同机部署时两个域名指向同一 IP，中继名表示服务身份，不要求修改该机器的系统 hostname。公网域与 MagicDNS 尾网域分开：例如服务使用 `example.net` 下的域名，设备使用 `tb.example.net`。端口方案见[安装说明](INSTALL.md#与控制面同机)。

## DERP 地域

DERP Map 在控制面的 `/etc/headscale/derp.yaml`。官方为自建中继预留 `900–999`，本项目按下表分配：[官方字段定义](https://github.com/tailscale/tailscale/blob/v1.104.5/tailcfg/derpmap.go)

| regionid | regioncode / 用途 |
| --- | --- |
| `900` | `home`，家庭中继 |
| `901` | `work`，工作地点或工区中继 |
| `902–909` | 预留给其他非云站点 |
| `910–999` | 云中继，从未占用的编号依次分配 |

每个逻辑区域一个中继。ID 分配后保持稳定，Map 键、区域和节点的 `regionid` 一致。代码和节点名在整张 Map 内唯一；新增独立站点时分配新 ID 和代码。

云中继的 `regioncode` 使用云平台完整地域 ID，不加厂商前缀，冲突时追加序号。例如 AWS 香港为 `ap-east-1`，阿里云香港为 `cn-hongkong`；机器名仍用 `hk`。具体值以 [AWS](https://docs.aws.amazon.com/global-infrastructure/latest/regions/aws-regions.html) / [阿里云](https://www.alibabacloud.com/help/en/user-center/developer-reference/common-region-id-reference) 配置为准，具体物理位置保留在部署资料中。

```yaml
regions:
  910:
    regionid: 910
    regioncode: ap-east-1
    regionname: Hong Kong
    nodes:
      - name: tb-derp-hk-01
        regionid: 910
        hostname: tb-derp-hk-01.example.net
        derpport: 443
        stunport: 3478
        canport80: false
```

域名、端口按实际服务填写；省略 `ipv4` 时使用 DNS。服务域名须在 MagicDNS 尾网域之外，并匹配证书。`910` 不固定绑定香港。

## 创建与维护

1. 普通节点传入控制地址和首次注册凭据即可；仅需指定名称时传 `NODE_HOSTNAME`，见[安装与配置](INSTALL.md#node-设备)。以 Headscale 中的最终设备名为准。
2. 新增中继先检查名称和 ID 冲突，再修改控制面的 Map，发布步骤见[运维](OPERATIONS.md#中继与访问策略)。不要用 `control --force` 覆盖已有多区域 Map。
3. 设备改名用 `tailscale set --hostname=<新名称>`，必要时再执行 `headscale nodes rename <新名称> --identifier <节点ID>`；保留状态，无需重新注册。[Tailscale](https://tailscale.com/docs/concepts/machine-names) · [Headscale](https://github.com/juanfont/headscale/blob/v0.29.7/cmd/headscale/cli/nodes.go)
4. DERP 只改名称时保留区域 ID；跨地域迁移或新旧并行时分配新 ID，确认客户端切换后再退役旧中继。

首次安装通过 control 的地域和节点名称 ENV 初始化 Map，见[安装说明](INSTALL.md#control-控制面)。这些名称不会修改已注册设备的名称。
