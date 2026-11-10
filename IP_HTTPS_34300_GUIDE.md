# AstrBot 战报管理 HTTPS 教程：公网 NAT 34300 与 HTTPS 34331

适用插件：v1.34.1 及以上。本教程以你实际的 Docker 输出和 NAT 映射为准。命令在**服务器宿主机 SSH 终端**执行，使用 root；Nginx 安装在宿主机，不进入 AstrBot 容器。

## 一、你的实际端口关系

### 已有 HTTP 入口

```text
浏览器 http://公网IP:34300
    → 服务商 NAT：公网34300 → 宿主机6185
    → Docker：宿主机6185 → astrbot容器6185
```

### 新的 HTTPS 入口

```text
浏览器 https://公网IP:34331/battle-report/
    → 服务商 NAT：公网34331 → 宿主机8443，TCP透传
    → 宿主机 Nginx：8443，处理HTTPS
    → 本机回源：http://127.0.0.1:6185/battle-report/
    → Docker：宿主机6185 → astrbot容器6185
```

| 端口 | 所在位置 | 用途 |
|---|---|---|
| **34300** | 服务商 NAT 的公网侧 | 原 HTTP 访问端口，转发至宿主机6185 |
| **6185** | 宿主机及 AstrBot 容器 | Docker实际映射6185到6185，是宿主机Nginx的后端 |
| **34331** | 服务商 NAT 的公网侧 | 新 HTTPS 访问端口，转发至宿主机8443 |
| **8443** | 宿主机 Nginx | 本机HTTPS监听端口，不等于公网端口 |

**Nginx 回源固定为 `http://127.0.0.1:6185`，浏览器访问固定使用 `https://公网IP:34331/battle-report/`。** 公网34300不应填进本机回源地址，公网也不直接使用8443。

本教程不修改已有 `34300 → 6185` NAT 映射，不改 Docker 的 `6185 → 6185` 映射。

## 二、确认 Docker 和本机 HTTP 服务

执行：

```bash
docker ps --format '{{.Names}}\t{{.Ports}}'
docker port astrbot 6185/tcp
```

按照你提供的实际配置，第二条输出应为：

```text
0.0.0.0:6185
[::]:6185
```

**这里看不到34300是正常的**，因为34300由服务商NAT转发，不是Docker发布的端口。

检查宿主机能否访问后端：

```bash
curl --noproxy '*' -I --connect-timeout 5 --max-time 10 http://127.0.0.1:6185/battle-report/
```

应返回200。这只读取页面，不发送密码。如果这里连接失败或404，先处理容器、插件版本或启用状态，再继续。不要用 `127.0.0.1:34300` 做本机后端检查。

确认宿主机8443空闲，或已经由此前配置的战报Nginx监听：

```bash
ss -lntp 'sport = :8443'
```

## 三、准备工具、脚本和公网 IP

确认宿主机工具：

```bash
python3 --version
openssl version
nginx -v
```

缺少工具时，Debian／Ubuntu 可执行：

```bash
apt-get update
apt-get install -y python3 openssl nginx
```

将插件更新至v1.33.0或以上后，复制脚本到宿主机：

```bash
docker cp astrbot:/AstrBot/data/plugins/astrbot_plugin_battle_report/scripts/setup_report_https.py /tmp/setup_report_https.py
```

先设置公网IP变量。下面 `203.0.113.10` 只是文档示例，请在执行前把单引号里的地址替换成你当前访问34300时使用的真实公网IP；保留变量名、等号和单引号，等号两侧不要加空格。

```bash
report_public_ip='203.0.113.10'
printf '当前公网IP：%s\n' "$report_public_ip"
```

执行后应直接显示你自己的IP，不会再弹出输入提示。只填纯IP，不加 `http://`、`https://`、端口或路径。

旧教程的 `read -r -p '提示语' report_public_ip` 中，引号内只是提示语；执行后还需要在终端另行输入IP。若终端现在仍在等待输入，先按 **Ctrl+C**，再执行上面的直接赋值命令。

证书签给**公网IP**，不要填写NAT内部IP、容器IP或127.0.0.1。保持当前SSH终端；如果重新连接，重新执行这段赋值命令。

## 四、生成证书，或复用已有证书

### 情况 A：还没有生成证书

执行下面这一条，宿主机监听8443、后端6185均已固定：

```bash
python3 /tmp/setup_report_https.py --ip "$report_public_ip" --output /opt/battle-report-https --https-port 8443 --upstream http://127.0.0.1:6185
```

脚本会显示CA指纹和文件SHA256。**脚本打印的8443是本机监听端口；在你的NAT环境中，公网访问端口应改用34331。**

实际公网地址为：

```text
https://你的公网IP:34331/battle-report/
```

输出目录非空时脚本会停止。不要删除已有CA或私钥，按情况B处理。

### 情况 B：之前已经生成过证书

端口变化不需要更换证书。保留 `/opt/battle-report-https/` 中的CA和私钥，先验证证书与当前公网IP：

```bash
openssl verify -CAfile /opt/battle-report-https/ca.crt -verify_ip "$report_public_ip" /opt/battle-report-https/server.crt
```

应看到 `server.crt: OK`。若IP不匹配、证书过期或文件缺失，先停止核对，不要直接重建CA。

如果曾按旧教程把回源改成34300，备份后改回**6185**：

```bash
cp -a /opt/battle-report-https/battle-report-ip-https.conf "/opt/battle-report-https/battle-report-ip-https.conf.backup-$(date +%Y%m%d-%H%M%S)"
sed -i 's#http://127\.0\.0\.1:34300#http://127.0.0.1:6185#g' /opt/battle-report-https/battle-report-ip-https.conf
```

已经使用6185时无需再次修改回源。

## 五、设置 NAT 环境的跳转并核对配置

在NAT环境中，Nginx必须保留相对跳转，避免将浏览器重定向到内部8443。无论情况A还是B，都执行下面这段；已有此设置时不会重复添加：

```bash
if ! grep -q 'absolute_redirect off;' /opt/battle-report-https/battle-report-ip-https.conf; then
    sed -i '/listen 8443 ssl;/a\    absolute_redirect off;' /opt/battle-report-https/battle-report-ip-https.conf
fi
```

查看配置：

```bash
rg 'listen|server_name|absolute_redirect|proxy_pass|proxy_set_header Host|X-Forwarded-Proto' /opt/battle-report-https/battle-report-ip-https.conf
```

没有 `rg` 时使用：

```bash
grep -E 'listen|server_name|absolute_redirect|proxy_pass|proxy_set_header Host|X-Forwarded-Proto' /opt/battle-report-https/battle-report-ip-https.conf
```

应包含：

```nginx
listen 8443 ssl;
absolute_redirect off;
server_name 你的公网IP;
proxy_set_header Host $http_host;
proxy_set_header X-Forwarded-Proto https;
```

**四处 `proxy_pass` 都必须为：**

```nginx
proxy_pass http://127.0.0.1:6185;
```

Nginx仍监听内部8443，不改成34331；34331由NAT负责。保留 `Host $http_host`，让后端收到浏览器原来的公网主机和端口。

## 六、备份并启用 Nginx

先备份已有同名配置，再安装本次配置：

```bash
if [ -f /etc/nginx/conf.d/battle-report-ip-https.conf ]; then
    cp -a /etc/nginx/conf.d/battle-report-ip-https.conf "/etc/nginx/conf.d/battle-report-ip-https.conf.backup-$(date +%Y%m%d-%H%M%S)"
fi
install -m 644 /opt/battle-report-https/battle-report-ip-https.conf /etc/nginx/conf.d/battle-report-ip-https.conf
```

只有配置检查成功后才重载或启动：

```bash
if nginx -t; then
    if systemctl is-active --quiet nginx; then
        systemctl reload nginx
    else
        systemctl start nginx
    fi
fi
```

成功应包含 `syntax is ok` 和 `test is successful`。检查失败时先处理报错；不要同时启用两份监听8443的重复战报配置。

需要开机启动时执行：

```bash
systemctl enable nginx
```

## 七、分别检查 NAT 与宿主机防火墙

服务商NAT面板应存在以下两条**TCP**映射：

| 公网端口 | 内部宿主机端口 | 用途 |
|---|---|---|
| 34300 | 6185 | 原HTTP后台，保留 |
| **34331** | **8443** | 新HTTPS入口 |

HTTPS映射使用TCP透传，不要把34331转到6185。NAT只转发TCP，不需要在NAT面板导入本教程的证书。

宿主机防火墙允许的是**内部8443**。服务商公网侧规则允许的是**外部34331**。仅开放防火墙不会自动建立NAT映射。

如果宿主机已经启用UFW：

```bash
ufw status
ufw allow 8443/tcp
```

不要直接执行 `ufw enable`，避免阻断未配置的SSH端口。也不需要给AstrBot容器增加8443的Docker映射，因为Nginx运行在宿主机。

## 八、先在服务器本机验证 HTTPS

确认本机监听：

```bash
ss -lntp 'sport = :8443'
```

在服务器本机检查证书和页面，**这里使用内部8443**：

```bash
curl --noproxy '*' --cacert /opt/battle-report-https/ca.crt --resolve "$report_public_ip:8443:127.0.0.1" -I --connect-timeout 5 --max-time 15 "https://$report_public_ip:8443/battle-report/"
```

应返回200。`--resolve` 将连接送到本机，同时仍按公网IP校验证书，不依赖服务商支持NAT回环。

检查根路径跳转：

```bash
curl --noproxy '*' --cacert /opt/battle-report-https/ca.crt --resolve "$report_public_ip:8443:127.0.0.1" -I --connect-timeout 5 --max-time 15 "https://$report_public_ip:8443/"
```

应有相对跳转头 `Location: /battle-report/`，不要出现带内部8443的绝对跳转。

本机验证成功、外部仍打不开时，检查 `34331 → 8443` NAT和公网侧规则。服务器可能不能访问自己的公网NAT入口，外部验证应使用你的电脑或手机。

## 九、在电脑／手机信任 CA，再从公网登录

通过已确认服务器身份的SSH／SFTP下载：

```text
/opt/battle-report-https/ca.crt
```

**只下载并分发 `ca.crt`，不要发送 `ca.key` 或 `server.key`。** 也可通过下面的公网地址下载证书，但信任前必须与SSH终端显示的指纹核对：

```text
https://你的公网IP:34331/battle-report-ca.crt
```

服务器上核对文件SHA256：

```bash
sha256sum /opt/battle-report-https/ca.crt
```

Windows的PowerShell可核对下载文件：

```powershell
Get-FileHash .\ca.crt -Algorithm SHA256
```

两边应一致。然后在设备导入信任：

- **Windows**：导入当前用户的“受信任的根证书颁发机构”，重开浏览器。
- **Android**：在系统安全设置安装CA证书，使用支持用户证书的系统浏览器。
- **iPhone／iPad**：安装证书描述文件后，在“通用 → 关于本机 → 证书信任设置”中信任该根证书。
- **macOS**：用“钥匙串访问”导入并信任核验过的证书。

QQ内置浏览器若不信任用户CA，可复制链接到系统浏览器。不要长期依靠跳过证书警告登录。

之后始终使用这个**公网地址**：

```text
https://你的公网IP:34331/battle-report/
```

- AstrBot管理员选择原管理员登录方式，使用原账号、密码及两步验证。
- 战队管理员使用网页账号密码，或私聊机器人 `/战报登录` 获取QQ登录码。

原 `http://公网IP:34300` 仍转到内部HTTP6185。不要改用 `https://公网IP:34300`，也不要从外网访问内部8443。

新34331入口只提供战报管理页面及所需认证，不是完整AstrBot后台。首次登录后，管理员原Cookie通常保持7天，战队会话固定7天并支持插件／AstrBot重启恢复。

## 十、按层定位问题

| 现象 | 检查 |
|---|---|
| 本机 `127.0.0.1:6185` 不通 | AstrBot容器、Docker6185映射、插件启用状态 |
| HTTPS显示502 | Nginx四处回源是否都是 `http://127.0.0.1:6185` |
| 本机8443不通 | Nginx是否监听8443，配置是否通过检查并已重载 |
| 本机HTTPS正常，公网34331不通 | NAT34331→8443是否生效，公网34331及内部8443是否放通 |
| 跳转后地址变为8443 | 是否加入 `absolute_redirect off;` 并安装、重载了实际配置 |
| HTTPS提示协议错误 | 是否误把34331映射到了HTTP6185，或误用https访问34300 |
| 证书不受信任／IP不匹配 | 当前设备是否信任正确CA，地址栏IP是否与证书一致 |
| 修改配置后无变化 | 是否将生成配置安装到 `/etc/nginx/conf.d/`，并通过检查后重载 |

本教程不改原HTTP NAT或Docker映射，不重启AstrBot／SnowLuma／Docker，不修改模型、代理或数据库。更完整的会话和证书说明见 [IP加密访问指南](https://github.com/1443290403/astrbot_plugin_battle_report/blob/master/IP_HTTPS_GUIDE.md)。
