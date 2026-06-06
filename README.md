# WeMai Client

> Windows 端的微信桥接客户端 —— 自动操控微信 GUI，通过 WebSocket 连到远端的 WeMai Adapter，把微信变成 LLM 的输入/输出设备。

## 是什么

**WeMai Client** 运行在你的 Windows 电脑上，它会：
1. 连接远端的 **WeMai Adapter** 的 WebSocket
2. 操控本机微信 GUI（通过 UIA 自动化）读取消息、发送消息、读写朋友圈
3. 把微信里的新消息实时推给 Adapter，同时接收 Adapter 发来的回复并打字发送

## 功能

| 模块 | 职责 |
|---|---|
| `main.py` | 入口，组件组装与生命周期管理 |
| `ws_client.py` | TCP 长连接，4 字节长度前缀 JSON 协议，断线自动重连 |
| `wx_listener.py` | 打开独立聊天窗口，轮询新消息，多选模式提取发送人 |
| `wx_sender.py` | 消费出站队列，向微信窗口打字/粘贴发送 |
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
| `pyautogui` | 模拟键盘粘贴 |
| `pywin32` | Windows API 绑定 |
| `Pillow` | 表情/图片截图处理 |

## 启动

首次运行会交互式询问服务器地址和端口，生成的 `config.toml` 已加入 `.gitignore`，不会泄漏到仓库。

```bash
python main.py
# 或双击
start.bat
```

## 配置

`config.toml`（首次运行自动生成）：

```toml
[connection]
server_host = "127.0.0.1"   # Adapter 所在 IP
server_port = 9721           # Adapter WS 端口
reconnect_delay = 5.0        # 断线重连间隔（秒）

[wechat]
target_chats = []            # 监听的聊天列表（空 = 等 Adapter 下发）
excluded = ["文件传输助手", "微信团队", "微信支付"]  # 忽略列表
send_delay = 0.2             # 每条消息发送间隔
close_weixin = false         # 是否自动关闭微信
include_muted = false        # 是否扫描免打扰聊天

[log]
level = "INFO"
file = "wemai-client.log"
```

`target_chats` 会从 Adapter 自动同步 —— Adapter 配置了哪些群聊/私聊名单，Client 就自动打开对应的独立窗口进行监听。

## 工作流程

1. **启动 → 连接**：Client 连上 Adapter 的 WebSocket，请求配置同步
2. **打开窗口**：根据 `target_chats` 打开所有独立聊天窗口，群聊自动激活多选模式
3. **监听轮询**：每秒轮询所有窗口的最新一条消息，发现新消息 → 去重 → 推送给 Adapter
4. **接收出站**：消费队列中的消息，一一打字/粘贴到微信窗口里发送
5. **朋友圈命令**：Adapter 发来的 `moment_read` / `moment_post` 由 `wx_moments.py` 执行
6. **好友请求**：每 60 秒检查一次新好友申请，推送给 Adapter 并通知管理员

特色机制：
- **去重**：MD5 哈希去重缓存（最近 10000 条），防止撤回/滚动导致重复推送
- **多选模式**：群聊独立窗口激活多选后，每条消息带发送人昵称，精准提取
- **表情截图**：emoji 消息通过截图 + base64 编码传给 Adapter
- **图片原文件**：image 消息通过 pyweixin 的 `save_media` 获取原图

## 依赖项目

- [pyweixin](https://github.com/Hello-Mr-Crab/pywechat) — 微信 UIA 自动化封装库（已 vendored 在 `pyweixin/` 目录中）

## 与 Adapter 的关系

| | WeMai Adapter | WeMai Client |
|---|---|---|
| 部署位置 | 服务器 (Linux/云) | 本地 Windows PC |
| 角色 | 被动监听 WS 连接 | 主动连接 Adapter |
| 操控对象 | MaiBot / LLM | 微信 GUI |
| 配置方式 | MaiBot WebUI | `config.toml` |
