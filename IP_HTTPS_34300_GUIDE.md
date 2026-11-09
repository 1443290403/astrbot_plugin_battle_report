# AstrBot 战报管理 HTTPS 教程：34300 映射到容器 6185

适用插件：v1.33.0 及以上。本教程按你确认的端口映射编写，所有命令在**服务器宿主机的 SSH 终端**执行，使用 root；不要进入 AstrBot 容器执行。

本教程使用宿主机 Nginx。Nginx 如果也运行在 Docker 容器中，`127.0.0.1` 指向的机器不同，不能直接套用这份地址。

## 一、固定使用的端口

| 端口 | 位置 | 用途 |
|---|---|---|
| **34300** | 宿主机 | 你现在访问 AstrBot 的 HTTP 端口，也是 Nginx 的后端端口 |
| **6185** | AstrBot 容器内 | AstrBot 内部服务，由 Docker 映射为宿主机 34300 |
| **8443** | 宿主机 Nginx | 本教程新增的 HTTPS 战报管理入口 |

访问路径：

```text
浏览器：https://你的公网IP:8443/battle-report/
    ↓ HTTPS 加密
宿主机 Nginx：8443
    ↓ 本机 HTTP
http://127.0.0.1:34300/battle-report/
    ↓ Docker 端口映射
AstrBot 容器：6185
```

**本教程的 `--upstream` 和 `proxy_pass` 一律使用 `http://127.0.0.1:34300`。** 不用容器内的 6185，不改现有 Docker 映射，也不把正在使用的 34300 改为 HTTPS 监听端口。

需要知道的公网 IP，就是你现在访问 `http://公网IP:34300` 时使用的那个 IP。下文只需输入一次 IP，端口不需要手动选择。

## 二、确认映射和服务正常

先执行：

```bash
docker ps --format '{{.Names}}\t{{.Ports}}'
docker port astrbot 6185/tcp
```

第二条输出应包含 `34300`，例如：

```text
0.0.0.0:34300
[::]:34300
```

你的容器名如果不是 `astrbot`，把后续命令里的容器名替换为实际名称。

然后检查宿主机能否通过 34300 访问战报页面：

```bash
curl --noproxy '*' -I --connect-timeout 5 --max-time 10 http://127.0.0.1:34300/battle-report/
```

应返回 `HTTP/1.1 200 OK` 或 `HTTP/1.1 200`。这只读取页面，不发送密码。如果这里连接失败、404 或超时，先解决容器运行、插件版本和端口映射问题，再继续配置 HTTPS。

`--noproxy '*'` 用于让本机检查直接访问本机，避免被已有的代理设置转发到其他地方。

确认8443空闲，或已经由此前配置的战报Nginx监听：

```bash
ss -lntp 'sport = :8443'
```

如果是其他服务占用8443，先确认端口安排，不要直接覆盖其配置。

## 三、准备宿主机工具和脚本

确认工具：

```bash
python3 --version
openssl version
nginx -v
```

只有缺少工具时，Debian／Ubuntu 可执行：

```bash
apt-get update
apt-get install -y python3 openssl nginx
```

将插件更新至 v1.33.0 或以上后，复制生成脚本到宿主机：

```bash
docker cp astrbot:/AstrBot/data/plugins/astrbot_plugin_battle_report/scripts/setup_report_https.py /tmp/setup_report_https.py
```

如果提示找不到文件，说明当前插件尚未包含此脚本，请先更新插件。

输入你访问 34300 时使用的公网 IPv4 地址，只输入 IP，不加协议、端口或斜杠：

```bash
read -r -p '请输入当前访问34300时使用的服务器公网IPv4地址: ' report_public_ip
```

保持当前 SSH 终端，后续检查继续使用这个变量。若重新连接，重新执行这条输入命令即可。

## 四、生成证书，或复用已有证书

### 情况 A：之前没有生成过证书

执行下面这一条，后端端口已经固定为 **34300**：

```bash
python3 /tmp/setup_report_https.py --ip "$report_public_ip" --output /opt/battle-report-https --https-port 8443 --upstream http://127.0.0.1:34300
```

成功后会显示访问地址、CA 证书指纹及文件 SHA256。访问地址应为：

```text
https://你的公网IP:8443/battle-report/
```

如果输出目录已存在且非空，脚本会停止以保护已有证书。不要删除目录或私钥来强行重新运行，按下面的“情况 B”处理。

### 情况 B：之前按旧教程生成过证书

如果 `/opt/battle-report-https/` 已有证书和 `battle-report-ip-https.conf`，**保留原证书**。先验证其 CA、有效期与当前 IP：

```bash
openssl verify -CAfile /opt/battle-report-https/ca.crt -verify_ip "$report_public_ip" /opt/battle-report-https/server.crt
```

必须看到：

```text
/opt/battle-report-https/server.crt: OK
```

若报 IP 不匹配、证书过期或文件缺失，先停止并核对，不要直接重建 CA。仅修正后端端口不需要更换证书，也不需要让设备重新导入信任。

验证通过后，备份原生成配置，并只把旧的本机后端 `6185` 改为 **34300**：

```bash
cp -a /opt/battle-report-https/battle-report-ip-https.conf "/opt/battle-report-https/battle-report-ip-https.conf.backup-$(date +%Y%m%d-%H%M%S)"
sed -i 's#http://127\.0\.0\.1:6185#http://127.0.0.1:34300#g' /opt/battle-report-https/battle-report-ip-https.conf
```

这一步只修改生成配置中的后端 URL，保留 CA 和服务器私钥。

## 五、核对并启用 Nginx

不论采用情况 A 还是 B，都检查以下内容：

```bash
rg 'listen|server_name|proxy_pass|X-Forwarded-Proto' /opt/battle-report-https/battle-report-ip-https.conf
```

宿主机没有 `rg` 时改用：

```bash
grep -E 'listen|server_name|proxy_pass|X-Forwarded-Proto' /opt/battle-report-https/battle-report-ip-https.conf
```

应看到：

```nginx
listen 8443 ssl;
server_name 你的公网IP;
proxy_set_header X-Forwarded-Proto https;
```

**所有 `proxy_pass` 都应是：**

```nginx
proxy_pass http://127.0.0.1:34300;
```

然后备份同名的已安装配置，再安装本次配置：

```bash
if [ -f /etc/nginx/conf.d/battle-report-ip-https.conf ]; then
    cp -a /etc/nginx/conf.d/battle-report-ip-https.conf "/etc/nginx/conf.d/battle-report-ip-https.conf.backup-$(date +%Y%m%d-%H%M%S)"
fi
install -m 644 /opt/battle-report-https/battle-report-ip-https.conf /etc/nginx/conf.d/battle-report-ip-https.conf
```

检查配置，只有检查成功后才会重载或启动 Nginx：

```bash
if nginx -t; then
    if systemctl is-active --quiet nginx; then
        systemctl reload nginx
    else
        systemctl start nginx
    fi
fi
```

检查成功应包含 `syntax is ok` 和 `test is successful`。若检查失败，停止并处理报错；正在运行的 Nginx 不会因为本段命令而重载错误配置。不要同时启用两份监听 8443 的重复战报配置。

需要随服务器开机启动时执行：

```bash
systemctl enable nginx
```

## 六、开放 8443，并从服务器验证 HTTPS

在服务器厂商的安全组／防火墙中允许 **TCP 8443 入站**。本教程没有新增容器服务，不需要给 AstrBot 增加 8443 的 Docker 端口映射。

如果服务器使用且已经启用 UFW，可执行：

```bash
ufw status
ufw allow 8443/tcp
```

不要为了这一步直接执行 `ufw enable`，以免未配置的 SSH 端口被阻断；未使用 UFW 时按现有防火墙工具设置。

确认 Nginx 已监听：

```bash
ss -lntp 'sport = :8443'
```

在服务器本机验证证书及 HTTPS 页面：

```bash
curl --noproxy '*' --cacert /opt/battle-report-https/ca.crt --resolve "$report_public_ip:8443:127.0.0.1" -I --connect-timeout 5 --max-time 15 "https://$report_public_ip:8443/battle-report/"
```

应返回 200。这里用公网 IP 校验证书，再通过 `--resolve` 连接本机 Nginx，避免服务器不能回环访问自己公网 IP 的问题。不要改成 `https://127.0.0.1:8443`，证书签给公网 IP 时这样会出现 IP 不匹配。

本机验证成功、外部仍连不上时，重点检查安全组和防火墙的 8443，而不是修改容器内 6185。

## 七、在电脑／手机信任证书，再登录

通过已确认服务器身份的 SSH／SFTP 下载这个文件：

```text
/opt/battle-report-https/ca.crt
```

也可下载 `https://你的公网IP:8443/battle-report-ca.crt`，但信任前必须与 SSH 终端显示的指纹核对。**只分发 `ca.crt`，不要下载或发送 `ca.key`、`server.key`。**

核对服务器上的 CA 文件 SHA256：

```bash
sha256sum /opt/battle-report-https/ca.crt
```

Windows 下载后可在 PowerShell 中核对：

```powershell
Get-FileHash .\ca.crt -Algorithm SHA256
```

两边结果应一致。然后导入：

- **Windows**：把证书导入当前用户的“受信任的根证书颁发机构”，重新打开浏览器。
- **Android**：在系统安全设置中安装 CA 证书，再使用支持用户证书的系统浏览器打开页面。
- **iPhone／iPad**：安装证书描述文件后，在“通用 → 关于本机 → 证书信任设置”中启用此根证书。
- **macOS**：使用“钥匙串访问”导入，并信任核验过的这张证书。

设备必须信任证书，地址栏也必须是正确的公网 IP。QQ 内置浏览器若不信任用户证书，可复制链接到系统浏览器。不要把“跳过证书警告”当作长期登录方案。

最后打开：

```text
https://你的公网IP:8443/battle-report/
```

- AstrBot 管理员：选择“AstrBot 管理员”，使用原账号密码及原两步验证。
- 战队管理员：使用自动创建的网页账号密码，或私聊机器人 `/战报登录` 获取 QQ 登录码。

**以后登录战报页面使用 8443 的 HTTPS 地址。** 原来的 `http://公网IP:34300` 仍是原 HTTP 后台；直接将它改为 `https://公网IP:34300` 不会成功，因为这个端口仍由 Docker 的 HTTP 映射占用。

8443 只提供本插件的战报管理入口和所需认证接口，不是完整 AstrBot 后台入口。首次在新地址登录一次后，原后台 Cookie 通常保持7天，战队会话固定7天并支持插件／AstrBot重启后恢复。

## 八、按端口定位常见问题

| 现象 | 对应检查 |
|---|---|
| 本机访问 `127.0.0.1:34300` 失败 | 检查 AstrBot 容器、`34300 → 6185` 映射、插件是否已启用 |
| HTTPS页面显示502 | 检查配置的后端是否全部为 `http://127.0.0.1:34300`，然后重新 `nginx -t` 并重载 |
| 8443连接超时 | 检查 Nginx监听、安全组及防火墙的TCP 8443 |
| 提示证书不受信任 | 核对CA指纹，并在当前设备信任正确的 `ca.crt` |
| 证书IP不匹配 | 核对生成时输入的公网IP；不要用127.0.0.1或其它IP作为浏览器入口 |
| 提示需要HTTPS | 访问 `https://公网IP:8443/battle-report/`，不是HTTP 34300 |
| 想再次修改代理配置 | 修改生成配置后还要安装到 `/etc/nginx/conf.d/`，并在 `nginx -t` 通过后重载 |

本教程不修改已有 34300 映射、不重启 AstrBot／SnowLuma／Docker，也不改模型或代理配置。证书和客户端信任必须按步骤实际部署后才生效。更完整的一周会话规则和证书说明见 [IP加密访问指南](https://raw.githubusercontent.com/1443290403/astrbot_plugin_battle_report/338218064cd7804615c3b1e9570c27ac6062726a/IP_HTTPS_GUIDE-1.33.0.md)。
