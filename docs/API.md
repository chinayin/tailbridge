# REST 管理

TailBridge 使用 Headscale 自带的 HTTPS REST API，入口为 `${SERVER_URL}/api/v1`，无需额外服务或 CLI。本文对应 **Headscale 0.29.7**；完整字段见部署实例的 `/swagger`、`/swagger/v1/openapiv2.json` 和[固定版本 schema](https://github.com/juanfont/headscale/blob/v0.29.7/gen/openapiv2/headscale/v1/headscale.swagger.json)。JSON 字段使用 camelCase，ID 返回为字符串。

## 认证

首次在控制面签发管理 API Key，保存到权限受限的文件；它拥有管理权限，与设备注册密钥不同：

```bash
umask 077
sudo headscale apikeys create --expiration 24h > headscale-api.key
```

需要远程管理时，通过已有安全通道传送该文件。在管理机的 Bash 会话设置入口和文件路径；下面的函数仅简化 curl 调用，认证头从 stdin 读取，密钥不会出现在命令参数中：

```bash
SERVER_URL=https://tailbridge.example.net
TB_API_KEY_FILE=./headscale-api.key
api() {
  local path="$1"
  shift
  { printf 'Authorization: Bearer '; cat "${TB_API_KEY_FILE}"; printf '\n'; } |
    curl -q --fail-with-body --silent --show-error --header @- \
      --header 'Content-Type: application/json' "$@" "${SERVER_URL%/}/api/v1${path}"
}
api /user
```

需要 curl、jq 和可信 TLS 证书；私有 CA 用 `--cacert` 指定，不跳过证书验证。不要开启 shell 跟踪或输出密钥响应到日志。未认证请求返回 401；curl 遇到 HTTP 错误返回非零。

轮换时先签发并验证新 key，再用 `sudo headscale apikeys list` 查旧 key ID，执行 `sudo headscale apikeys expire --id <旧ID>`。也可通过 REST 的 `POST /apikey`（`expiration`）、`GET /apikey` 和 `POST /apikey/expire`（`id`）完成轮换。

## 批准客户端注册

客户端执行 `tailscale up --login-server=<控制面URL>` 后，注册页面会给出 Auth ID 和 `headscale auth register` 命令；该命令供控制面管理员执行。按本项目流程，在管理电脑完成[认证](#认证)后调用等价 REST API，无需 SSH：

```bash
api /auth/register -X POST \
  --data '{"user":"alice","authId":"<注册页面上的Auth ID>"}' |
  jq '.node | {id,givenName,ipAddresses,user:.user.name}'
```

`user` 填已有用户名，`authId` 填完整的 `hskey-authreq-...`。管理员确认设备及所属用户后批准；管理 API Key 只保留在管理电脑。成功后客户端自动接入，用 `tailscale status` 检查；请求失效时重新发起登录，取得新的 Auth ID。

## 用户与注册密钥

先查询用户；已存在时直接使用其 ID，否则创建：

```bash
api '/user?name=alice'
TB_USER_ID=$(api /user -X POST --data '{"name":"alice"}' | jq -er '.user.id')
TB_EXPIRATION=$(jq -nr 'now + 3600 | todateiso8601')
umask 077
api /preauthkey -X POST --data "$(jq -nc \
  --arg user "${TB_USER_ID}" --arg expiration "${TB_EXPIRATION}" \
  '{user:$user,expiration:$expiration,reusable:false,ephemeral:false}')" > preauthkey.json
jq -er '.preAuthKey.key' preauthkey.json > node-auth.key
TB_KEY_ID=$(jq -er '.preAuthKey.id' preauthkey.json)
```

每台新设备签发一个单次使用的 key，安全传给[节点安装流程](INSTALL.md#node-设备)的 `NODE_AUTHKEY`，注册后删除本地密钥文件。密钥到期不撤销已经建立的节点身份，也不授予设备互访权限。

```bash
api /preauthkey | jq '.preAuthKeys[] | {id,user:.user.name,used,expiration}'
api /preauthkey/expire -X POST --data "{\"id\":\"${TB_KEY_ID}\"}"
api "/preauthkey?id=${TB_KEY_ID}" -X DELETE
```

用户改名使用 `POST /user/{用户ID}/rename/{新名称}`；删除使用 `DELETE /user/{用户ID}`，先处理该用户的节点。`preauthkey` 的过期和删除参数是 **key ID**，不是 key 明文或用户名。[上游接口定义](https://github.com/juanfont/headscale/blob/v0.29.7/proto/headscale/v1/headscale.proto)

## 节点与路由

先查询实际节点 ID；示例中的 `7` 需替换。查询时仅显示需要的字段，避免把嵌套注册密钥写入日志：

```bash
api /node | jq '.nodes[] | {id,givenName,ipAddresses,availableRoutes,approvedRoutes}'
api /node/7 | jq '.node | {id,givenName,online,expiry}'
api /node/7/rename/tb-node-hk-01 -X POST | jq '.node | {id,givenName}'
api /node/7/approve_routes -X POST --data '{"routes":["0.0.0.0/0","::/0"]}' |
  jq '.node | {id,approvedRoutes}'
```

`availableRoutes` 是节点宣告的路由；`approve_routes` **替换完整批准列表**。保留其他路由时一并提交，撤销全部批准发送 `{"routes":[]}`。出口仍需 Linux 转发、策略授权和客户端主动选择，见[出口配置](OPERATIONS.md#配置-linux-出口)。

下列操作会使目标设备需要重新认证或注册，确认 ID 后使用：

```bash
api /node/7/expire -X POST | jq '.node | {id,expiry}'
api /node/7 -X DELETE
```

策略保留 `policy.mode: file`，DERP Map 也由文件维护；修改和重载步骤统一见[中继与访问策略](OPERATIONS.md#中继与访问策略)。管理 API 不替代访问授权。

REST 验证命令与范围见 [E2E 说明](E2E_TEST_GUIDE.md)。
