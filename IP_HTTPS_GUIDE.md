# 仅 IP 的 HTTPS 与一周登录

适用版本：v1.34.0。没有域名也能使用 HTTPS。此方案生成私有 CA 和包含 IP 的服务器证书，由 Nginx 加密浏览器到服务器的连接；**每台使用设备需要导入信任 `ca.crt`**。如果已有可信机构签发的 IP 证书，也可改用现成证书。生成脚本不修改系统配置、不启动服务、不开放端口。

公网端口可能由服务商NAT转发，不等于Docker的宿主机端口。对于 **公网34300→宿主机6185、HTTPS公网34331→宿主机8443**，请使用 [NAT专用教程](IP_HTTPS_34300_GUIDE.md)，Nginx回源固定为6185。

## 1. 生成证书和配置

宿主机需有 Python3、OpenSSL 和 Nginx。下面的 `203.0.113.10` 只是文档示例，执行前替换成你的真实公网IP；端口 `6185` 按实际Docker输出调整。变量赋值只改单引号内的值，不加协议或路径，等号两侧不要加空格。更新插件后，在服务器执行：

```bash
docker cp astrbot:/AstrBot/data/plugins/astrbot_plugin_battle_report/scripts/setup_report_https.py /tmp/setup_report_https.py
report_public_ip='203.0.113.10'
report_backend_port='6185'
python3 /tmp/setup_report_https.py --ip "$report_public_ip" --output /opt/battle-report-https --https-port 8443 --upstream "http://127.0.0.1:$report_backend_port"
```

容器名不是 `astrbot` 时替换为实际名称。宿主机端口以 `docker port astrbot 6185/tcp` 的输出为准，不能只按浏览器公网URL判断：若输出6185，Nginx回源就填6185；公网34300可能只是NAT端口。若Docker实际发布的是34300，才填34300。

默认在宿主机另用 HTTPS **8443**。有NAT时还需配置公网HTTPS端口→内部8443，外部地址使用NAT公网端口；NAT部署按专用教程加入相对跳转设置，不能假定公网也能直接访问8443。避免影响已有443服务；可通过 `--https-port` 选择其他未占用端口。输出目录必须为新目录或空目录，不能位于插件目录中。已有证书目录不会覆盖，重新运行请保留原 CA 与私钥，勿随意换 CA 导致所有设备重新信任。

生成内容：

- `ca.crt`：需要导入设备的根证书，可公开。
- `ca.key`、`server.key`：私钥，权限0600，仅留在服务器，不发送给其他人。
- `server.crt`：服务器证书，含指定 IP，有效期365天。
- `battle-report-ip-https.conf`：可检查后安装的 Nginx 配置，只代理本插件页面、登录及战报 API，其他核心 API 不开放。

脚本会输出访问地址、CA证书指纹和 `ca.crt` 文件SHA256。保留这些输出，用于核验客户端收到的证书。

## 2. 启用 HTTPS

先查看生成的配置，确认 IP、端口、证书路径和后端端口正确。首次部署、没有同名配置时执行：

```bash
install -m 644 /opt/battle-report-https/battle-report-ip-https.conf /etc/nginx/conf.d/battle-report-ip-https.conf
nginx -t && systemctl reload nginx
```

已有同名配置请先备份、比较，不要覆盖其他站点。Nginx 必须已启动；安全组及服务器防火墙需允许所选端口的 TCP 入站。此方案不需要修改 AstrBot／SnowLuma 容器、不重启 Docker、不更改模型或代理配置。

默认入口：

```text
https://你的服务器IP:8443/battle-report/
```

使用实际端口和 IP，必须以 `https://` 开头。普通 `http://IP:6185` 仍是原后台 HTTP，不能把地址栏文字变化当作服务器已开通 TLS。

## 3. 在使用设备信任证书

通过已经确认服务器身份的 SSH／SFTP 下载 `/opt/battle-report-https/ca.crt`。也可下载 `https://IP:8443/battle-report-ca.crt`，但在信任前必须对照服务器终端输出核验；不要直接信任未知网页提供的根证书。

Windows 可用 PowerShell `Get-FileHash .\ca.crt -Algorithm SHA256` 核对文件SHA256；Linux／macOS 可用 `openssl x509 -in ca.crt -noout -fingerprint -sha256` 核对证书指纹。

- **Windows**：将 `ca.crt` 导入当前用户的“受信任的根证书颁发机构”，重新打开浏览器。
- **macOS**：导入“钥匙串访问”，仅对这张核验过的证书设置信任。
- **Android**：在系统安全设置中安装 CA 证书，使用系统 Chrome 等支持用户证书的浏览器。
- **iPhone／iPad**：安装证书描述文件后，在“通用 → 关于本机 → 证书信任设置”中开启该根证书信任。

只导入 `ca.crt`，不导入私钥。浏览器应显示证书有效、IP匹配后再输入密码。部分 QQ 内置浏览器不信任用户导入的 CA，可复制链接到系统浏览器；不要长期依靠跳过证书警告登录。

## 4. 登录保持规则

- **AstrBot 管理员**：使用原后台认证及两步验证、原 `Secure`／`HttpOnly` Cookie，当前支持版本通常有效7天；HTTPS下刷新、关闭浏览器后重开可恢复。AstrBot重启时原JWT密钥仍有效即可继续使用。插件不保存管理员密码、不签发核心JWT，也不改原认证策略。
- **战队管理员密码／QQ码登录**：登录后的会话固定7天，服务端仅保存随机令牌摘要，浏览器使用 `Secure`／`HttpOnly` Cookie。刷新、浏览器重开和插件／AstrBot重启都可在有效期内恢复。
- QQ登录码本身仍只有5分钟、限用一次；未使用的码不会跨重启保留。
- 会记住最近成功的登录类型。战队会话失效后不会自动切到同浏览器里残留的 AstrBot 管理员登录。
- 退出、改密、停用密码或撤销授权仍按原权限规则失效；不会因为一周有效期放宽访问范围。账号密码文件和会话摘要需保密备份，不放到公开站点。

密码传输加密由 HTTPS/TLS 完成；数据库中的密码继续使用加盐 PBKDF2 哈希。Base64、前端字符串变换或单纯哈希不代替传输加密。本机／SSH隧道的 localhost HTTP 可继续使用，SSH外网段由隧道加密；公网不能使用这项回环例外。

## 5. 常见问题

| 现象 | 检查 |
|---|---|
| 8443打不开 | Nginx是否启动、配置检查是否通过、端口是否占用及防火墙／安全组是否放行 |
| 502 | Nginx能否访问 `http://127.0.0.1:宿主机端口/battle-report/`，是否误填容器端口 |
| 证书不被信任 | 是否在当前设备／浏览器信任了正确CA，指纹是否与服务器一致 |
| IP不匹配 | 生成证书时的IP是否与地址栏相同；更换IP需要用原CA重新签发对应服务器证书 |
| 刷新仍需登录 | 是否使用HTTPS、Cookie是否被浏览器禁止／退出时清理、是否已过期或撤权 |
| 后台 Pages 中“密码账号”不见了 | 通过HTTPS或SSH访问原后台；普通公网HTTP会隐藏敏感凭据操作 |

服务器证书到期前应使用原 CA 续签服务器证书并重载 Nginx；保留 `ca.key`，续签不必让设备重新导入 CA。使用可信公共证书时按签发方要求续期。TLS 配置和客户端信任必须实际部署后才能生效，安装插件不会替服务器自动完成这些步骤。
