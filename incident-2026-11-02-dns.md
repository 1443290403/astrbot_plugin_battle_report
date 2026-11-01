# 机器人图片与对话无响应故障复盘

日期：2026-11-02  
时区：Asia/Shanghai（北京时间）  
状态：用户已确认图片与对话全部恢复  
依据：本次提供的日志、服务器诊断输出及修复后的用户反馈

## 1. 结论

本次故障的主要已确认原因是：**部署环境的默认 DNS 解析链路异常，SnowLuma 无法解析 QQ 媒体上传域名 `htdata3.qq.com`，导致图片和语音上传失败。**

诊断中，直接向两个公共 DNS 发起的默认 UDP 查询均超时；对同一域名启用 TCP DNS 后成功返回地址，使用阿里云 HTTPS DNS 也成功。这说明 TCP 和 HTTPS 解析路径可用，故障集中在默认解析路径及其 UDP 查询链路。

最终通过在 SnowLuma 和 AstrBot 容器中添加 `options use-vc`，让系统解析器使用 TCP 查询 DNS，恢复了媒体上传；随后将 `dns_opt: [use-vc]` 写入 Compose 服务配置，供后续重建容器时使用。用户最终确认全部恢复。

**尚未查明的是：哪个具体环节导致了 UDP 查询失败，以及它为什么在当时发生。** 本次没有取得防火墙变更记录、宿主机转发规则对照或 DNS 抓包，因此不能将故障进一步确定为某个云厂商策略、Docker 缺陷或某次配置修改。

## 2. 环境与影响

| 项目 | 已知信息 |
|---|---|
| AstrBot | 日志显示 v4.27.3，容器名 `astrbot` |
| SnowLuma | 容器名 `snowluma`，镜像标签为 `motricseven7/snowluma:latest`；标签本身不能确定实际软件版本 |
| 编排文件 | `/opt/bot/docker-compose.yml` |
| OneBot 反向连接 | `ws://astrbot:6199/ws` |
| 容器 DNS | `127.0.0.11`，Docker 内置解析器 |
| Docker 显示的外部 DNS | `223.5.5.5`、`223.6.6.6` |
| 主要失败域名 | `htdata3.qq.com` |

受影响的表现包括：

- `/排行` 的图片已经生成，但无法上传到 QQ。
- 部分正常回复文本被转换成 TTS 语音后，语音同样无法上传，用户看不到回复。
- 期间出现过模型接口请求超时，以及 OneBot API 暂时不可用。
- 后续测试消息被并入尚未结束的 Agent 会话，看起来像机器人一直不响应。

本次 DNS 修复没有涉及战报数据表、比分、积分或发送标记的修改。

## 3. 关键日志与时间线

以下时间均来自已提供的北京时间日志。未记录准确恢复时刻，因此不计算精确故障时长。

| 时间或阶段 | 证据 | 含义 |
|---|---|---|
| 00:32—00:33 | `/排行` 进入发送阶段，随后图片上传报错，`cmdId=1004`、`getaddrinfo EAI_AGAIN htdata3.qq.com` | 图片已进入上传流程，失败发生在域名解析阶段 |
| 00:48—00:49 | SnowLuma 多次记录连接 `ws://astrbot:6199/ws` 成功 | OneBot 连接并非始终不可用 |
| 00:52 左右 | `All chat models failed: APITimeoutError: Request timed out.` | 当时存在真实的模型接口请求超时 |
| 00:52 左右 | 超时提示被转换为 TTS，发送时报 `ApiNotAvailable` | 模型请求失败后，错误提示的发送也遇到了问题 |
| 00:52—00:53 | `Captured follow-up message for active agent run` | 新消息已被接收，但被加入仍在运行的会话；仅凭这行不能认定死锁 |
| 重启 AstrBot 后，00:59 左右 | SnowLuma 连接 6199 端口出现 `ECONNREFUSED` | 该时刻目标端口未接受连接，与重启后的启动窗口相吻合 |
| 01:00—01:01 | OneBot 再次返回明确的图片上传错误；正常回复文本进入 TTS，随后语音上传报 `cmdId=1008`、同一域名 `EAI_AGAIN` | API 通道已能返回结果；图片和语音都受同一 DNS 故障影响 |
| DNS 诊断阶段 | 默认解析失败，直接公共 DNS 查询超时；TCP DNS 和 HTTPS DNS 成功 | 找到了可用的解析路径 |
| 修复后 | 用户确认“已经全部恢复” | 图片与对话的实际使用已恢复 |

错误含义需要分别理解：

| 错误 | 本次应如何理解 |
|---|---|
| `EAI_AGAIN` | 系统域名解析暂时失败；它本身不说明丢包发生在哪一层 |
| `ETIMEOUT` | 指定的 DNS 查询在等待时间内未得到结果 |
| `APITimeoutError` | 模型 API 请求超时，需要结合模型接口和网络日志判断原因 |
| `ApiNotAvailable` | aiocqhttp 当时没有可用于完成该请求的 API 通道 |
| `ECONNREFUSED` | 目标地址已解析，但目标端口拒绝连接，常见于服务尚未监听或正在重启 |

## 4. 为什么看起来像“LLM 没有回复”

本次存在两条不同的失败路径。

第一条是模型 API 确实超时，日志中有明确的 `APITimeoutError`。DNS 异常可能影响模型域名解析或请求建立，但本次没有对具体模型接口做逐请求对照，不能证明每次模型超时都由同一个 DNS 问题造成。

第二条是**正常回复文本已经生成并进入 TTS，但生成的语音无法上传**。日志中的发送链只有 `Record` 语音组件，这种情况下，即使上游已经产出文本，用户也可能收不到可见的文字回复。

```text
排行数据 → 生成图片 ──────────────┐
                                 ├→ 上传 QQ 媒体 → 解析 htdata3.qq.com 失败 → 用户收不到媒体
正常回复文本 → TTS → 生成语音 ───┘
```

因此，看到“没有回复”时，需要同时检查回复生成、TTS 转换和最终发送三个阶段。期间临时关闭自动／强制 TTS，可以帮助验证普通文字收发是否正常；TTS 并非本次已确认的 DNS 根因。

## 5. 定位 DNS 故障的证据

### 5.1 默认解析失败

SnowLuma 容器使用系统解析器查询：

```bash
timeout 12s docker exec snowluma node -e "require('dns').lookup('htdata3.qq.com',{all:true},(e,a)=>console.log(e?e.code:a))"
```

结果为 `EAI_AGAIN`。

宿主机执行以下命令，在本次检查中也没有输出解析地址：

```bash
timeout 10s getent ahostsv4 htdata3.qq.com
```

该次未记录退出码，所以不能进一步断定宿主机这条命令究竟是超时还是其他解析失败。

### 5.2 直接 UDP 查询也超时

使用 Node.js `dns.Resolver` 直接指定公共 DNS 查询同一域名，得到：

```text
223.5.5.5    ETIMEOUT
119.29.29.29 ETIMEOUT
```

这表明问题不能仅归结为 Docker 内置 DNS 的域名缓存：绕过内置解析器后，默认 UDP 查询同样没有得到结果。

### 5.3 外网并未完全中断

容器的 TCP 连接检查显示：

```text
223.5.5.5:443 TCP 正常
1.1.1.1:443   TCP 超时
```

这说明至少部分外网 TCP 路径可用。它不能证明所有外网地址都可访问，也不能仅凭一个地址超时认定整个容器没有网络。

### 5.4 TCP DNS 和 HTTPS DNS 成功

在新启动的诊断进程中设置：

```text
RES_OPTIONS=use-vc timeout:2 attempts:1
```

同一域名成功返回多个 IPv4 地址。阿里云 HTTPS DNS 的检查也返回 HTTP 200、DNS `Status: 0`，并带有有效 A 记录。

这些对照支持本次选择：**保留原有 DNS 地址，改用已验证可用的 TCP 查询路径。** 返回的 CDN 地址会变化，本次没有通过写死 QQ 上传服务器 IP 来修复。

## 6. 实际修复方法

### 6.1 即时修复现有容器

以下命令先在各自容器内备份解析配置，然后追加 `use-vc`。原有 `nameserver 127.0.0.11` 保留，以继续解析 `astrbot` 等 Docker 服务名。

```bash
for dns_container in snowluma astrbot; do
  docker exec -u 0 "$dns_container" sh -c '
    set -e
    test -f /tmp/resolv.conf.before-tcp || cp /etc/resolv.conf /tmp/resolv.conf.before-tcp
    grep -q "^options.*use-vc" /etc/resolv.conf || printf "\noptions use-vc\n" >> /etc/resolv.conf
  ' || break
  printf '%s\n' "${dns_container}: TCP DNS 已启用"
done
```

`use-vc` 让使用该配置的系统解析器通过 TCP 查询 DNS。它改变的是解析传输方式，QQ 图片、语音和模型 API 的业务请求仍使用各自原来的协议。

这项即时修改作用于当前容器，不能代替编排文件中的持久配置。宿主机自身的 DNS 配置也没有因此自动改变。

### 6.2 写入 Compose 配置

先备份原文件：

```bash
cp -a /opt/bot/docker-compose.yml "/opt/bot/docker-compose.yml.bak-$(date +%Y%m%d-%H%M%S)"
```

在原有 `snowluma` 和 `astrbot` 两个服务下，分别合并以下 `dns_opt` 字段，其余镜像、挂载、端口和环境变量保留：

```yaml
services:
  snowluma:
    # 保留已有服务配置，在同一层级添加：
    dns_opt:
      - use-vc

  astrbot:
    # 保留已有服务配置，在同一层级添加：
    dns_opt:
      - use-vc
```

上面是字段位置示例，不能用它替换整份 Compose 文件。如果已有 `dns_opt` 列表，将 `use-vc` 加入原列表即可。

验证配置：

```bash
cd /opt/bot
docker compose -f docker-compose.yml config --quiet
```

本次检查只提示顶层 `version` 属性已过时，没有报告配置错误。这个提示不影响运行，可单独移除顶层 `version:` 行。

**保存配置与应用配置是两件事：** 修改 Compose 文件不会立即修改现有容器；本次当前容器由即时修复生效，`dns_opt` 会在之后按该配置重建容器时应用。

### 6.3 修复后验证

先使用普通进程检查，不额外指定 `RES_OPTIONS`，确认容器本身的配置已经生效：

```bash
docker exec snowluma node -e "for(const h of ['htdata3.qq.com','astrbot'])require('dns').lookup(h,{all:true},(e,a)=>console.log(h,e?e.code:a))"
```

检查 AstrBot 容器的系统解析：

```bash
docker exec astrbot python3 -c "import socket; print(socket.getaddrinfo('htdata3.qq.com',443))"
```

随后按实际功能验证：

1. 发送 `/排行`，确认图片真正到达 QQ。
2. 发送一句普通聊天，确认回复可见。
3. 若此前关闭了 TTS，恢复需要的 TTS 设置，再确认语音可发送。
4. 检查新日志，确认本次请求没有再次出现域名解析和媒体上传错误。

用户已确认最终全部恢复。尚未单独记录未来容器重建后的验证结果，后续重建时应再执行上述检查。

## 7. 插件改动与其他日志的关系

排查期间发布过战报插件 v1.20.3，尝试直接等待图片发送并在明确上传失败时回退文字。这种方式只能绕开部分媒体发送失败，无法恢复服务器 DNS，而且改变了原有响应流程。

随后发布 v1.20.4，撤回排行直接发送和停止事件的改动，恢复图片与点评经过 AstrBot 标准回复链，保留其他插件在回复阶段接入的位置。发送标记、战绩统计与数据库 schema 保持原规则。

现有证据不足以证明 v1.20.3 导致了全局 LLM 无响应；同样，恢复标准回复链也不能证明部署网络已恢复。**此次图片与语音恢复的关键网络修复，是启用已验证可用的 TCP DNS。**

其他日志应分别处理：

- AstrBot 重启后短暂出现的 6199 端口拒绝连接，需要结合启动进度判断。后续明确的 OneBot 上传错误说明 API 通道后来已经能返回结果。
- QQ Crashpad 的 `.lock: File exists` 信息没有被证实是本次上传失败的原因。
- Compose 的 `version is obsolete` 是配置格式提示，没有阻止配置检查。

## 8. 预防与后续事项

| 事项 | 建议 |
|---|---|
| 保留持久配置 | 将 `use-vc` 保留在两个服务的 `dns_opt` 中，升级或重建后重新验证 |
| 保留内部服务发现 | 继续使用 Docker 内置解析器，使 `astrbot`、`mysql` 等服务名能按原网络配置解析 |
| 验证实际发送 | 同时测试文字、图片和语音；图片生成成功或模型生成成功不等于消息已送达 |
| 分层查看日志 | 按模型请求、TTS、OneBot 连接、媒体上传和 DNS 分别定位，避免把所有现象归为模型无响应 |
| 宿主机 DNS | 本次容器修复未验证宿主机解析已恢复；宿主机后续下载、拉取镜像等操作需单独检查 |
| 追查最初触发原因 | 如需彻底定位，检查故障前后的宿主机防火墙、Docker 转发/NAT、云网络策略及 UDP/TCP 53 抓包；当前没有足够证据确定是哪一项 |

如果未来需要回滚当前容器的即时修改，可以使用对应容器内的 `/tmp/resolv.conf.before-tcp` 备份恢复 `/etc/resolv.conf`；该备份位于容器临时目录，容器重建后不保证保留。Compose 中的持久设置需要另行调整。恢复旧解析路径后，应重新验证域名解析，避免重新进入本次故障状态。

本次复盘最重要的排查顺序是：**先确认消息在哪一层失败，再验证默认 DNS、TCP DNS 和实际媒体发送，最后把已验证有效的修复写入持久配置。**
