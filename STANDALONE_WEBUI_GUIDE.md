# 独立战报管理入口

适用版本：v1.33.1，AstrBot >= 4.27.3。默认复用现有后台服务；公网密码登录需HTTPS，已有HTTPS后台无需另配代理。

独立页面包含本地战报、战报核对、云端归档及 QQ 发送，不加载 AstrBot 后台导航。登录页支持**战队管理员账号密码**、**QQ一次性登录码**，以及原 AstrBot 管理员账号。两种战队登录方式的设置和授权见 [TEAM_WEB_LOGIN_GUIDE.md](TEAM_WEB_LOGIN_GUIDE.md)；原后台登录仍支持两步验证。

后端仍由正在运行的 AstrBot 插件提供，复用原数据库及 OneBot 连接。原 AstrBot 管理员保留原权限；战队管理员登录在服务端限制为其 QQ 当前负责的战队，不能访问后台设置。各管理员使用自己的账号或QQ登录码。

## 默认方式：安装后直接访问

安装／更新并启用插件后，在**原 AstrBot 后台地址后添加 `/battle-report/`**，例如：

```text
https://服务器IP:8443/battle-report/
```

已有HTTPS后台时保持原地址与端口，仅追加路径即可。公网HTTP不再提交登录信息；只有IP时见 [IP加密访问指南](IP_HTTPS_GUIDE.md)。选择自己的登录方式即可。这里没有新端口，不需要修改 Docker 映射、手动导出或配置 Nginx；页面会随插件更新，AstrBot 重启后自动恢复。停用／卸载时移除本实例路由。原后台 API 仍使用 AstrBot 鉴权；新增战报专用会话仅可访问战报范围接口。登录／会话校验在插件内完成，其他公开资源仅为登录页及静态文件。

内置与手动导出入口均不提交公网HTTP登录信息。已有HTTPS后台沿用原地址；仅HTTP／IP时，按IP加密访问指南配置证书与代理。本机或SSH回环可使用localhost HTTP。若更换域名，可按下面的可选方式配置。你的原后台若不能从外部访问，需先解决该地址本身的网络访问；插件不会自动修改防火墙或 Docker。

日志出现“独立战报入口已自动注册”表示挂载成功。若框架接口不兼容，日志会提示，原后台 Pages 页面仍可用。

## 可选部署：手动导出独立静态站

以下内容只适用于想单独部署静态站或独立域名的情况，默认入口无需执行。

### 1. 导出独立页面

先将插件更新至 v1.33.1。以下命令在服务器终端执行，容器名沿用 `astrbot`；若实际名称不同，请替换。

```bash
docker exec astrbot python3 /AstrBot/data/plugins/astrbot_plugin_battle_report/scripts/export_report_webui.py --output /tmp/battle-report-web-1.33.1
mkdir -p /opt/battle-report-web-1.33.1
docker cp astrbot:/tmp/battle-report-web-1.33.1/. /opt/battle-report-web-1.33.1/
```

导出脚本只复制静态页面，不复制数据库配置、后端代码或凭据；拒绝覆盖非空输出目录。重复执行时可换一个新的临时输出路径。也可将发布包 `battle-report-web-1.33.1.zip` 解压到 `/opt/battle-report-web-1.33.1/`，该目录下应直接存在 `index.html`、`app.js`、`standalone.js` 等文件。

下面两种入口选一种即可。模板默认 AstrBot 在宿主机 `127.0.0.1:6185` 可访问；若改过后台端口，需修改模板中的五个 `proxy_pass`。独立静态站的页面文件和代理必须在**同一地址**，不能把静态页单独放到另一域名后直接跨域调用接口。

### 2A. 没有域名：SSH 转发

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

### 2B. 有域名：HTTPS 独立站点

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
| `root` | 导出的静态页面目录，默认 `/opt/battle-report-web-1.33.1` |
| 五处 `proxy_pass` | 实际 AstrBot 后台地址，默认 `http://127.0.0.1:6185` |

保存后执行 `nginx -t`，通过后执行 `systemctl reload nginx`。浏览器直接访问 `https://你的战报域名`，使用原 AstrBot 账号登录。防火墙／安全组需允许网站的 80、443 端口。模板将 HTTP 跳转到 HTTPS，手动导出的独立静态站不允许公网 HTTP 登录。

## 3. 登录、退出与更新

- 首次使用先在 AstrBot 原后台完成初始化、改密或账号安全升级，再登录独立页。登录错误、两步验证仍由 AstrBot 判断，独立页不绕过这些检查。
- 后台JWT或战队令牌仅存在当前页面内存中，不写入 localStorage、sessionStorage 或 URL。通过HTTPS使用有效HttpOnly Cookie，刷新／浏览器重开可恢复；战队会话固定7天并保存摘要，插件／AstrBot重启也可恢复。AstrBot管理员沿用原后台Cookie和JWT有效期，通常7天。非认证Cookie仅记录最近成功的登录方式，恢复失败不自动改用其他账号。
- 登录过期时重新登录，再手动重试刚才的操作。页面不会自动重放提交、核对或发送请求，避免重复操作。退出会调用对应登录服务的退出接口并清空页面；下次需显式登录。
- 默认内置入口随插件自动更新，无需导出。手动静态站更新时，重新导出对应版本到新目录，修改 Nginx 的 `root`，检查后重载即可。原 AstrBot 后台 Pages 入口继续使用官方 bridge，不受此导出影响。
- Nginx 只转发原后台登录／退出、本插件核对 API 以及战报登录／专用API，其他 `/api/` 请求返回 404。插件 API 仍要求 AstrBot 认证；此配置没有开放匿名数据接口。

## 常见问题

| 现象 | 检查 |
|---|---|
| 默认入口 404 | 是否为 v1.33.1 及以上且已启用；地址末尾为 `/battle-report/`；查看注册日志，重启或重新启用插件 |
| 手动静态站 404 | `root` 下是否直接有 `index.html`；域名／端口是否匹配这个 server 块 |
| 登录或查询 502 | 宿主机能否连接 `127.0.0.1:6185`，AstrBot 是否启动；如换端口需同步五处代理 |
| 提示接口没有有效 JSON | 原登录／插件API及 `/battle-report/auth/`、`/battle-report/api/comparison/` 是否按当前模板代理到 AstrBot |
| 提示必须 HTTPS | 已有HTTPS后台直接使用；只有IP时按IP加密访问指南配置证书／代理并在设备导入信任，或使用SSH回环。内置与静态入口均不提交公网HTTP登录信息 |
| 登录成功但数据库未连接 | 检查原插件数据库状态；独立页面不另外配置 MySQL |
| QQ 发送连接为空 | 检查 AstrBot 中 OneBot／SnowLuma 连接；QQ 官方 OpenID 不能作为私人 QQ 发送目标 |

验证范围：自动入口在隔离环境使用 AstrBot v4.27.3 的兼容适配器、鉴权中间件和密码／登录／JWT 代码，验证静态路由、登录／退出、过期不重放、查询／复制／下载／模拟发送；冷启动、热安装及新旧实例清理有回归覆盖。可选 Nginx 方案此前通过真实 Nginx 的 HTTP／HTTPS 验证。数据库及 OneBot 使用样本模拟，未部署到你的服务器，未发送真实 QQ 消息。
