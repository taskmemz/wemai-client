# WeMai Client

> Windows 端的微信桥接客户端 —— 自动操控微信 GUI，通过 WebSocket 连到远端的 WeMai Adapter，把微信变成 LLM 的输入/输出设备。

## ⚠️ 分支说明

**本分支（master）为 WeFlow 模式版本**，通过 WeFlow HTTP API 实现消息收发，无需操控微信 GUI。

> **如果你需要纯 pyweixin 实现（不依赖 WeFlow），请切换到 [`pure-uia`](../../tree/pure-uia) 分支下载。**

⚠️ `pure-uia` 版本存在以下已知差异：
- emoji 表情通过截图以图片形式识别，无法从微信内直接获取表情包数据
- 不支持 WeFlow 模式的数据源切换
- 部分功能（如图片解密、朋友圈读取）依赖 UIA 自动化，稳定性和速度不如 WeFlow
- 发送仍依赖 pyweixin 的 UIA 操控，WeFlow 版本的发送路径不同

---

## 是什么

**WeMai Client** 运行在你的 Windows 电脑上，它会：
1. 连接远端的 **WeMai Adapter** 的 WebSocket
2. 操控本机微信 GUI（通过 UIA 自动化）读取消息、发送消息、读写朋友圈
3. 把微信里的新消息实时推给 Adapter，同时接收 Adapter 发来的回复并打字/粘贴发送
4. 自动检测好友请求并上报，等待 LLM 决策后批准或忽略
5. 语音消息自动右键 → "语音转文字" → 以 `[语音]xxx` 形式送达 LLM

## 功能

| 模块 | 职责 |
|---|---|
| `main.py` | 入口，组件组装与生命周期管理 |
| `ws_client.py` | TCP 长连接，4 字节长度前缀 JSON 协议，渐进退避重连 |
| `wx_listener.py` | 打开独立聊天窗口，轮询新消息，多选模式提取发送人 |
| `wx_sender.py` | 消费出站队列，向微信窗口打字/文件发送 |
| `wx_moments.py` | 读取/发布朋友圈 |

消息流动方向：

```
Adapter ─(WS)─→ ws_client ─→ outbound_queue ─→ wx_sender ─→ 微信 GUI
微信 GUI ─→ wx_listener ─→ ws_client ─(WS)─→ Adapter
```

## 环境要求

- **Windows**（需要微信桌面版运行）
- **Python 3.10+**
- 微信已登录

## 安装

```bash
cd wemai-client
pip install -r requirements.txt
```

| 包 | 用途 |
|---|---|
| `pywinauto` | UIA 自动化操控微信窗口 |
| `pyautogui` | 模拟键盘操作 |
| `pywin32` | Windows API 绑定 |
| `Pillow` | 表情/图片截图处理 |

## 启动

首次运行会交互式询问服务器地址和端口，生成的 `config.toml` 已加入 `.gitignore`，不会泄漏到仓库。

```bash
python main.py
```

## 配置

`config.toml` 仅保留本地连接信息，其余全部由 Adapter 在 WebSocket 连接建立后自动下发：

```toml
[connection]
server_host = "127.0.0.1"   # Adapter 所在 IP
server_port = 9721           # Adapter WS 端口
reconnect_delay = 5.0        # 断线重连基准（秒）

[log]
level = "INFO"
file = "wemai-client.log"
format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
```

| 配置来源 | 内容 |
|----------|------|
| 本地 `config.toml` | 连接地址、日志参数 |
| Adapter 推送 | 数据源模式（WeFlow/pyweixin）、WeFlow 连接信息、聊天过滤名单、管理员、发送间隔、排除会话等 |

配置首次运行时交互式生成。连接建立后 Adapter 会全量推送配置，客户端根据 `data_source` 字段自动选择 WeFlow 或 pyweixin 数据源启动。

## 数据源模式

| 模式 | 数据来源 | 适用场景 |
|------|---------|---------|
| **pyweixin** | UIA 自动化操控微信 GUI | 需要 pyweixin 包 + 桌面微信登录 |
| **WeFlow** | WeFlow HTTP API + SSE 推送 | WeFlow 后台运行，无需 GUI 操作 |

WeFlow 是一个完全本地的微信聊天记录实时查看与导出工具，提供 HTTP API 和 SSE 推送。Client 通过 WeFlow 接口接收消息（无需 GUI 操作），图片直接解密返回。

下载地址：
- 主项目（Electron 版）：<https://github.com/Nixer-2301/WeFlowBackup/releases>
- Rust CLI 预览版：<https://github.com/334456777/WeFlow/releases/tag/nightly-preview>

两种模式的发送均走 `pyweixin` 的 `send_messages_to_friend`（Adapter 自动将 WeFlow 收到的 wxid 转为显示名称后再传给 Client）。

## 工作流程

1. **启动 → 连接**：Client 连上 Adapter 的 WebSocket，发送 `sync_config` 请求配置
2. **等待配置下发**：Adapter 推送完整配置（数据源模式、过滤名单、参数等），Client 据此选择启动 WeFlow 或 pyweixin 数据源
3. **模式切换**：若 Adapter 热重载中切换了 `data_source.mode`，Client 自动停止当前数据源并启动新模式
4. **监听/推送**：数据源检测到新消息 → 去重 → 推送给 Adapter
5. **接收出站**：消费队列中的消息，文本用 `send_messages_to_friend`，图片/GIF 用 `send_files_to_friend` 发送
6. **朋友圈命令**：Adapter 发来的 `moment_read` / `moment_post`，WeFlow 模式走 API，pyweixin 模式走 GUI 自动化
7. **好友请求**：每 60 秒检查一次新好友请求 → 已添加/已过期的跳过 → 待验证的推送给 Adapter
8. **语音消息**：检测到语音消息 → 右键 → "语音转文字" → 提取文字以 `[语音]xxx` 发送给 Adapter

## 特色机制

| 机制 | 说明 |
|---|---|
| **去重** | `_KNOWN_IDS` 集（上限 10000）防止同类消息重复推送 |
| **好友去重** | `_SEEN_FRIEND` 集（上限 1000）防止同一好友请求重复上报 |
| **多选模式** | 群聊独立窗口自动激活多选，`_parse_multiselect_text` 匹配群成员清单精准提取发送人 |
| **语音转文字** | 私聊走独立窗口右键，群聊走主窗口右键，自动触发"语音转文字"菜单 |
| **表情截图** | emoji 消息截图 + base64 编码传 Adapter，作为文本段中的图片发送 |
| **图片原文件** | image 消息通过 pyweixin 的 `save_media` 获取原图 |
| **渐进退避** | WS 断线后重连间隔从 `reconnect_delay` 开始线性递增到 30 秒上限 |
| **窗口重调** | 独立窗口自动调到 931×767，消息位置一致 |

## 依赖项目

- [pywechat](https://github.com/Hello-Mr-Crab/pywechat) — 微信 UIA 自动化封装库（已 fork 在 `pyweixin/` 目录中，LGPL-2.1）

## 与 Adapter 的关系

| | WeMai Adapter | WeMai Client |
|---|---|---|
| 部署位置 | 服务器 (Linux/云) | 本地 Windows PC |
| 角色 | 被动监听 WS 连接 | 主动连接 Adapter |
| 操控对象 | MaiBot / LLM API | 微信 GUI |
| 配置方式 | MaiBot WebUI | `config.toml` |
