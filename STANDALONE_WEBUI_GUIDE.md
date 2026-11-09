# 独立战报管理入口

适用版本：v1.30.0，AstrBot >= 4.27.3，Docker 部署、宿主机 Nginx。

独立页面包含本地战报、战报核对、云端归档及 QQ 发送，不加载 AstrBot 后台导航。使用**原 AstrBot 管理员用户名和密码**登录；启用两步验证时填写动态验证码或恢复码。这里不使用 SSH 密码，也不用填写数据库密码。

后端仍由正在运行的 AstrBot 插件提供，复用原数据库及 OneBot 连接。此入口供你本人或已有 AstrBot 管理员使用，权限与原后台账号相同；没有独立战队账号或服务端战队授权。不要把同一个管理员账号作为多战队分权方案。

## 1. 导出独立页面

先将插件更新至 v1.30.0。以下命令在服务器终端执行，容器名沿用 `astrbot`；若实际名称不同，请替换。

```bash
docker exec astrbot python3 /AstrBot/data/plugins/astrbot_plugin_battle_report/scripts/export_report_webui.py --output /tmp/battle-report-web-1.30.0
mkdir -p /opt/battle-report-web-1.30.0
docker cp astrbot:/tmp/battle-report-web-1.30.0/. /opt/battle-report-web-1.30.0/
```

导出脚本只复制静态页面，不复制数据库配置、后端代码或凭据；拒绝覆盖非空输出目录。重复执行时可换一个新的临时输出路径。也可将发布包 `battle-report-web-1.30.0.zip` 解压到 `/opt/battle-report-web-1.30.0/`，该目录下应直接存在 `index.html`、`app.js`、`standalone.js` 等文件。

下面两种入口选一种即可。模板默认 AstrBot 在宿主机 `127.0.0.1:6185` 可访问；若改过后台端口，需修改模板中的三个 `proxy_pass`。页面文件和代理必须在**同一地址**，不能把静态页单独放到另一域名后直接跨域调用接口。

## 2A. 没有域名：SSH 转发

在服务器执行：

```bash
docker cp astrbot:/AstrBot/data/plugins/astrbot_plugin_battle_report/deployment/nginx-loopback.conf /etc/nginx/conf.d/battle-report.conf
nginx -t
```

显示配置检查成功后再执行：

```bash
systemctl reload nginx
```

模板只监听服务器 `127.0.0.1:8088`。随后在**自己电脑**的终端执行，替换 SSH 端口、用户名和服务器地址：

```bash
ssh -p 20155 -N -L 8088:127.0.0.1:8088 root@服务器IP
```

保持该终端连接，在自己电脑浏览器打开：

```text
http://127.0.0.1:8088
```

此方案无需开放 8088 公网端口。若电脑 8088 已占用，将 `-L` 后第一个 `8088` 改成 `18088`，浏览器相应访问 `http://127.0.0.1:18088`。

## 2B. 有域名：HTTPS 独立站点

先把自己的域名解析到服务器，并准备有效 HTTPS 证书。在服务器执行：

```bash
docker cp astrbot:/AstrBot/data/plugins/astrbot_plugin_battle_report/deployment/nginx-https.conf /etc/nginx/conf.d/battle-report.conf
nano /etc/nginx/conf.d/battle-report.conf
```

修改以下位置：

| 配置 | 修改为 |
|---|---|
| 两处 `server_name reports.example.com` | 你的战报域名 |
| `ssl_certificate` | 该域名证书的实际完整链文件路径 |
| `ssl_certificate_key` | 该域名私钥文件路径 |
| `root` | 导出的静态页面目录，默认 `/opt/battle-report-web-1.30.0` |
| 三处 `proxy_pass` | 实际 AstrBot 后台地址，默认 `http://127.0.0.1:6185` |

保存后执行 `nginx -t`，通过后执行 `systemctl reload nginx`。浏览器直接访问 `https://你的战报域名`，使用原 AstrBot 账号登录。防火墙／安全组需允许网站的 80、443 端口。模板将 HTTP 跳转到 HTTPS，不能用公网 HTTP 输入密码。

## 3. 登录、退出与更新

- 首次使用先在 AstrBot 原后台完成初始化、改密或账号安全升级，再登录独立页。登录错误、两步验证仍由 AstrBot 判断，独立页不绕过这些检查。
- JWT 仅存在当前页面内存中，不写入 localStorage、sessionStorage 或 URL。若 AstrBot 提供有效 HttpOnly Cookie，刷新页面可复用；否则刷新后重新登录。
- 登录过期时重新登录，再手动重试刚才的操作。页面不会自动重放提交、核对或发送请求，避免重复操作。退出会调用 AstrBot 退出接口并重新加载页面。
- 更新插件后，重新导出对应版本到新目录，修改 Nginx 的 `root`，检查后重载即可。原 AstrBot 后台 Pages 入口继续使用官方 bridge，不受此导出影响。
- Nginx 只转发登录／退出及本插件核对 API，其他 `/api/` 请求返回 404。插件 API 仍要求 AstrBot 认证；此配置没有开放匿名数据接口。

## 常见问题

| 现象 | 检查 |
|---|---|
| 打开后 404 | `root` 下是否直接有 `index.html`；域名／端口是否匹配这个 server 块 |
| 登录或查询 502 | 宿主机能否连接 `127.0.0.1:6185`，AstrBot 是否启动；如换端口需同步三处代理 |
| 提示接口没有有效 JSON | `/api/auth/login` 与 `/api/plug/astrbot_plugin_battle_report/comparison/` 是否按模板代理到 AstrBot |
| 提示必须 HTTPS | 使用有效 HTTPS 域名，或从自己电脑通过 SSH 转发访问 `127.0.0.1`；公网 HTTP 登录被禁用 |
| 登录成功但数据库未连接 | 检查原插件数据库状态；独立页面不另外配置 MySQL |
| QQ 发送连接为空 | 检查 AstrBot 中 OneBot／SnowLuma 连接；QQ 官方 OpenID 不能作为私人 QQ 发送目标 |

验证范围：隔离环境使用真实 Nginx、AstrBot v4.27.3 的密码校验／登录／JWT 代码和插件接口，数据库及 OneBot 使用样本模拟。未部署到你的服务器，未发送真实 QQ 消息。
