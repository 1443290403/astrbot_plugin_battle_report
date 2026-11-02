# AstrBot 插件安装包

源码见 master 分支；本分支仅保存安装包和下载附件。请下载 ZIP 文件，在 AstrBot 插件管理中上传。

最新版本：v1.25.1

- 文件：astrbot_plugin_battle_report-1.25.1.zip
- 源码提交：4a35daac45e248b05d5e45483bab471a1633d664
- 根目录包含 metadata.yaml 与 main.py
- 已使用 AstrBot v4.27.3 安装包检查方法校验，所有文件与源码提交一致
- SHA256：c7cbcd38cb51e762538ea4a1300f380df6716bd2783be0d8bec091c06b105a47

旧版本安装包保留，按文件名选择。

## 趋势图预览

- [横轴两端留白](trend-preview-1.21.3.png)

## 战绩卡片预览

- [综合战绩与多 ID 同图对照](record-preview-1.21.2.png)

## 故障复盘文档

- [2026-11-02 图片与对话无响应故障复盘（Markdown）](incident-2026-11-02-dns.md)

## QQ 官方等车

- [v1.24.0 配置与使用说明](OFFICIAL_QUEUE_GUIDE-1.24.0.md)

## QQ / OpenID 账号关联

- [v1.25.0 账号关联、权限与等车说明](OFFICIAL_QUEUE_GUIDE-1.25.0.md)

## 加载兼容性修复

v1.24.0 / v1.25.0 存在等车过滤器注册写法错误，AstrBot v4.27.3 请使用 v1.25.1 或后续修复版本。

v1.25.1 ZIP 已通过 v4.27.3 实际自定义过滤器/LLM 注册函数的隔离模块导入检查，另有 975 项回归测试通过。
