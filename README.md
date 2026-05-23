# WeMai Client — 微信桥接客户端 🤖💬

> **把你的微信变成 MaiBot 的眼睛和手。**
>
> _Turn your WeChat into MaiBot's eyes and hands._

[中文](#中文) · [English](#english)

---

## 中文

### 📖 这是什么

**WeMai Client** 是一个跑在你 **Windows 电脑** 上的 Python 脚本。它通过 `pyweixin`（基于 pywinauto 的 UI 自动化库）直接操控微信界面——打开窗口、读消息、敲字、按回车、看朋友圈、发朋友圈。

它通过 WebSocket 连接云服务器上的 **WeMai Adapter（MaiBot 插件）**，把你微信里的一切实时同步给 MaiBot。

```
你:  发了一条消息 → WeChat
                       ↓
WeMai Client 轮询窗口 → 发现新消息
                       ↓
发送 JSON → WebSocket → 云服务器 → MaiBot → LLM 思考
                       ↑
MaiBot 回复 ← WebSocket ← JSON ← Adapter
                       ↓
WeMai Client 收到 → 往微信窗口打字 → Enter
                       ↓
对方收到: "叫我懒就行"
```

### ✨ 能干什么

| 功能 | 怎么做到的 |
|------|-----------|
| 💬 **收发消息** | 轮询独立窗口 → `window_text()` 读新消息；`type_keys("内容{ENTER}")` 打字发送 |
| 😄 **表情包 GIF** | 检测到 `window_text() == "动画表情"` → 标记为 emoji 类型 |
| @ **艾特人** | Adapter 下发的 `at_members`，在消息前输入 `@昵称 ` |
| 📱 **朋友圈读取** | 调用 `Moments.dump_recent_posts()` → 结果发给 MaiBot |
| 📝 **朋友圈发布** | MaiBot 发 `moment_post` 命令 → 调 `Moments.post_moments(text)` |
| 🔎 **全局扫描** | 每 30s 用 `scan_for_new_messages` 扫一遍会话列表，发现新聊天自动开窗口加入监控 |
| 🔄 **断线重连** | WS 断开后自动重连，配置可调间隔 |
| 👤 **自我识别** | 通过消息气泡在窗口中的左右位置判断是自己发的还是对方发的 |

### 🧩 文件结构

```
wemai-client/
├── main.py              #  入口：编排所有组件
├── config.toml          #  配置文件（改这里）
├── config.py            #  配置读取器
├── ws_client.py         #  WebSocket 客户端（连 Adapter）
├── wx_listener.py       #  核心：轮询微信窗口的消息
├── wx_sender.py         #  核心：往微信窗口打字发消息
├── wx_moments.py        #  朋友圈读写封装
├── requirements.txt     #  依赖清单
└── pyweixin/            #  微信 UI 自动化库（不用动）
```

### ⚡ 快速开始

#### 1️⃣ 安装依赖

```bash
pip install -r requirements.txt
```

#### 2️⃣ 改配置

编辑 `config.toml`：

```toml
[connection]
server_host = "你的云服务器IP"
server_port = 9721

[wechat]
# 留空 = 扫描所有会话
target_chats = []
# 排除不想管的
excluded = ["文件传输助手", "微信团队"]
```

#### 3️⃣ 运行

```bash
python main.py
```

看到类似这样的日志就成功了：

```
已连接到插件服务器 xxx.xxx.xxx.xxx:9721
从 Adapter 同步到监听目标: ['某不知名的赵']
动态打开窗口: 某不知名的赵
检测到 [某不知名的赵] 某不知名的赵: 你好 (text)
```

### ⚙️ 全部配置

```toml
[connection]
server_host = "127.0.0.1"        # Adapter 地址
server_port = 9721               # Adapter 端口
reconnect_delay = 5              # 断线重试间隔（秒）

[wechat]
target_chats = []                # 指定监听（空=全部）
excluded = ["文件传输助手"]       # 排除名单
send_delay = 0.2                 # 发送间隔
close_weixin = false             # 退出时关微信
```

### 🏗️ 内部架构

```
┌─────────────────────────────────────────────┐
│              WeMai Client                     │
│                                              │
│  wx_listener 线程 ── 每 1s 轮询独立窗口 ──→ │
│    ↓ detect new msg                         │
│    ↓ _parse_message() → (type, sender, ...) │
│    ↓ _emit() → put to asyncio.Queue         │
│                                              │
│  outbound_processor 线程 ← pull from Queue  │
│    ↓ send via ws_client.send_inbound()      │
│                                              │
│  ws_client.run() ── reads WS responses ──→  │
│    ↓ "outbound" → wx_sender 线程            │
│    ↓ "config_update" → listener.update()    │
│    ↓ "moment_read" → WeChatMoments.read()   │
│    ↓ "moment_post" → WeChatMoments.post()   │
│                                              │
│  wx_sender 线程 ← outbound_queue            │
│    ↓ edits[0].set_focus()                   │
│    ↓ type_keys("消息{ENTER}")              │
│    ↓ _on_post_send → mark_sent()            │
└─────────────────────────────────────────────┘
```

### 🧠 怎么判断消息是谁发的

私聊中自己发的和对方发的在文本上没区别，靠 **UIA 元素位置** 区分：

```
┌───────────────────────┐
│                        │
│  ┌──┐                 │  ← 对方的泡（靠左）
│  │文本  │                 │
│  └──┘                 │
│                        │
│                 ┌──┐  │  ← 自己的泡（靠右）
│                 │文本│  │
│                 └──┘  │
│                        │
└───────────────────────┘
```

消息元素的 `rectangle.center_x` > 聊天列表的 `rectangle.center_x` → 自己发的，跳过。

### 🤝 依赖

- Python 3.10+
- pywinauto
- pyweixin（已内置在项目目录中）
- psutil、pywin32

### ⚠️ 注意事项

1. **微信必须登录** — 脚本不会帮你扫码
2. **UI 可见性** — WeChat 4.1+ 首次运行前建议开一次"讲述人"模式解锁 UI
3. **RDP 兼容** — 远程桌面最小化窗口也能工作（用 `set_focus` 替代 `click_input`）
4. **不要频繁操作** — pyweixin 有延迟参数，设太短会被微信弹"为了你的账号安全"
5. **`pyweixin/` 目录** — 是修改过的版本（移除了免打扰过滤），不要替换回原版

---

## English

### 📖 What Is This

**WeMai Client** is a Python script that runs on your **Windows PC**. It uses `pyweixin` (a pywinauto-based UI automation library) to directly control the WeChat interface: opening windows, reading messages, typing text, pressing Enter, browsing moments, and posting moments.

It connects via WebSocket to the **WeMai Adapter (MaiBot plugin)** on your cloud server, bridging your WeChat in real-time to MaiBot.

```
You send a message → WeChat
                       ↓
WeMai Client polls the window → detects new message
                       ↓
Sends JSON → WebSocket → Cloud Server → MaiBot → LLM thinks
                       ↑
MaiBot replies ← WebSocket ← JSON ← Adapter
                       ↓
WeMai Client receives → types into WeChat → Enter
                       ↓
Friend receives: "Call me Lan"
```

### ✨ Features

| Feature | How It Works |
|---------|-------------|
| 💬 **Message relay** | Poll dialog windows → `window_text()` for new messages; `type_keys("text{ENTER}")` to send |
| 😄 **GIF stickers** | Detects `window_text() == "动画表情"` → marks as emoji type |
| @ **Mentions** | Adapter sends `at_members` → prepends `@name ` to message |
| 📱 **Read moments** | Calls `Moments.dump_recent_posts()` → results to MaiBot |
| 📝 **Post moments** | MaiBot sends `moment_post` command → calls `Moments.post_moments(text)` |
| 🔎 **Global scan** | Every 30s scans session list for new chats → auto-opens windows |
| 🔄 **Auto-reconnect** | Drops then reconnects with configurable interval |
| 👤 **Self-identification** | Uses message bubble position (left=incoming, right=outgoing) to filter own messages |

### ⚡ Quick Start

#### 1️⃣ Install deps

```bash
pip install -r requirements.txt
```

#### 2️⃣ Edit config

```toml
[connection]
server_host = "your-server-ip"
server_port = 9721

[wechat]
target_chats = []     # empty = all chats
excluded = ["File Transfer"]
```

#### 3️⃣ Run

```bash
python main.py
```

Look for:

```
已连接到插件服务器 xxx.xxx.xxx.xxx:9721
从 Adapter 同步到监听目标: ['某不知名的赵']
检测到 [某不知名的赵] 某不知名的赵: hello (text)
```

### ⚙️ Full Config

```toml
[connection]
server_host = "127.0.0.1"       # Adapter address
server_port = 9721              # Adapter port
reconnect_delay = 5             # Reconnect delay (sec)

[wechat]
target_chats = []               # Specific chats (empty=all)
excluded = ["File Transfer"]    # Excluded chats
send_delay = 0.2                # Send interval
close_weixin = false            # Close WeChat on exit
```

### 🧠 Identity Detection

In private chats, incoming and outgoing messages look the same in the text. The client distinguishes them via **UIA element position**:

```
┌───────────────────────┐
│                        │
│  ┌──┐                 │  ← Incoming bubble (left)
│  │text│                 │
│  └──┘                 │
│                        │
│                 ┌──┐  │  ← Outgoing bubble (right)
│                 │text│  │
│                 └──┘  │
│                        │
└───────────────────────┘
```

If the message item's `rectangle.center_x` > the chat list's `rectangle.center_x` → it's our own message, skip it.

### ⚠️ Notes

1. **WeChat must be logged in** — no QR scanning automation
2. **UI visibility** — Run Windows Narrator for 5min before first use (WeChat 4.1+)
3. **RDP friendly** — Works in minimized RDP sessions (`set_focus` replaces `click_input`)
4. **Don't spam** — Short delays may trigger "re-login for account safety"
5. **`pyweixin/` is modified** — Don't replace with the original (mute filter removed)

---

**WeMai Client** — 你的微信，MaiBot 的第六感。
_Your WeChat, MaiBot's sixth sense._

Built with ❤️ by the MaiBot community.
