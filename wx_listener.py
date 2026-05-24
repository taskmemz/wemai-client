from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("wemai_client.listener")


def _find_latest_media_file(time_window: float = 10.0) -> str | None:
    """在 WeChat 聊天文件目录中查找最近收到的媒体文件（GIF/图片）"""
    try:
        from pyweixin import Tools
        import glob as file_glob
        chat_base = Tools.where_chatfile_folder()
        if not chat_base or not os.path.isdir(chat_base):
            return None
        now = time.time()
        candidates = []
        for root, dirs, files in os.walk(chat_base):
            for f in files:
                fpath = os.path.join(root, f)
                try:
                    mtime = os.path.getmtime(fpath)
                except OSError:
                    continue
                if now - mtime < time_window and f.lower().endswith((".gif", ".jpg", ".jpeg", ".png", ".webp")):
                    candidates.append((fpath, mtime))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]
    except Exception:
        return None


_KNOWN_IDS: set[str] = set()
_KNOWN_LOCK = threading.Lock()


def _dedup_key(chat: str, sender: str, content: str) -> str:
    raw = f"{chat}|{sender}|{content}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _is_known(key: str) -> bool:
    with _KNOWN_LOCK:
        if key in _KNOWN_IDS:
            return True
        _KNOWN_IDS.add(key)
        if len(_KNOWN_IDS) > 10000:
            _KNOWN_IDS.clear()
        return False


class WeChatListener:
    def __init__(
        self,
        target_chats: list[str],
        excluded: list[str],
        poll_interval: float,
        close_weixin: bool,
        send_delay: float,
        on_message: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._target_chats = list(target_chats)
        self._excluded = set(excluded)
        self._poll_interval = poll_interval
        self._close_weixin = close_weixin
        self._send_delay = send_delay
        self._on_message = on_message
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._dialog_windows: dict[str, Any] = {}
        self._my_name = ""
        self._last_seen: dict[str, Any] = {}
        self._sending: set[str] = set()
        self._send_lock = threading.RLock()
        self._Navigator: Any = None
        self._Monitor: Any = None
        # 记录每个聊天是否为群聊，chat_name -> bool
        self._chat_group_status: dict[str, bool] = {}

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="wx-listener")
        self._thread.start()
        logger.info("监听线程已启动（独立窗口模式）")

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5)

    def merge_remote_targets(self, enable_filter: bool, group_list: list[str], private_list: list[str]) -> None:
        remote = group_list + private_list
        if not remote:
            return
        added = [c for c in remote if c not in self._target_chats]
        if added:
            self._target_chats.extend(added)
            logger.info("从 Adapter 同步到监听目标: %s", added)

    def get_dialog_windows(self) -> dict[str, Any]:
        return self._dialog_windows

    def begin_send(self, chat_name: str) -> None:
        """发送前标记，防止回显检测"""
        with self._send_lock:
            self._sending.add(chat_name)

    def mark_sent(self, chat_name: str) -> None:
        """发送后更新 last_seen，清除发送标记"""
        dw = self._dialog_windows.get(chat_name)
        if dw is None:
            with self._send_lock:
                self._sending.discard(chat_name)
            return
        time.sleep(0.8)  # RDP 下等 UI 更新
        try:
            chat_list = dw.child_window(control_type="List")
            if chat_list.exists(timeout=0.3):
                items = chat_list.children(control_type="CheckBox") or chat_list.children(control_type="ListItem")
                if items:
                    self._last_seen[chat_name] = items[-1].element_info.runtime_id
        except Exception:
            pass
        with self._send_lock:
            self._sending.discard(chat_name)

    def _run(self) -> None:
        try:
            from pyweixin import Contacts, GlobalConfig, Monitor, Navigator
        except ImportError as e:
            logger.critical("导入 pyweixin 失败: %s", e)
            return

        GlobalConfig.close_weixin = self._close_weixin
        GlobalConfig.send_delay = self._send_delay
        GlobalConfig.load_delay = 1.0
        self._Navigator = Navigator
        self._Monitor = Monitor

        # 记录当前微信窗口实际尺寸，避免 open_weixin 强制缩小到 (1000,1000)
        try:
            import win32gui
            hwnd = win32gui.FindWindow('Qt51514QWindowIcon', '微信')
            if hwnd == 0:
                hwnd = win32gui.FindWindow('Qt51514QWindowIcon', 'Weixin')
            if hwnd:
                rect = win32gui.GetWindowRect(hwnd)
                w, h = rect[2] - rect[0], rect[3] - rect[1]
                if w > 200 and h > 200:
                    GlobalConfig.window_size = (w, h)
        except Exception:
            pass

        try:
            Navigator.open_weixin()
            logger.info("微信主窗口已打开")
        except Exception as e:
            logger.error("打开微信失败: %s", e)
            return

        try:
            info = Contacts.check_my_info(close_weixin=False)
            self._my_name = info.get("昵称", "") or info.get("微信号", "")
            logger.info("当前账号: %s", self._my_name)
        except Exception:
            logger.warning("获取个人信息失败")

        chats = list(self._target_chats)
        if chats:
            self._open_all_dialog_windows(chats)
        else:
            logger.info("target_chats 为空，等待 Adapter 同步监听目标...")

        last_heartbeat = time.time()
        last_global_scan = 0.0
        while self._running:
            try:
                now = time.time()
                self._poll_all_windows(self._last_seen)
                self._open_pending_windows()
                if now - last_global_scan >= 30:
                    self._global_scan(Navigator, Monitor)
                    last_global_scan = now
            except Exception as e:
                logger.error("轮询异常: %s", e)
            now = time.time()
            if now - last_heartbeat >= 60:
                logger.info("运行中: %d 个窗口", len(self._dialog_windows))
                last_heartbeat = now

    def _open_all_dialog_windows(self, chats: list[str]) -> None:
        opened = 0
        for chat in chats:
            if chat in self._excluded or chat in self._dialog_windows:
                continue
            try:
                dw, is_group = self._Navigator.open_seperate_dialog_window(
                    friend=chat, window_minimize=False, close_weixin=False,
                    return_is_group=True,
                )
                self._dialog_windows[chat] = dw
                self._chat_group_status[chat] = is_group
                opened += 1
                if opened % 5 == 0:
                    time.sleep(0.5)
            except Exception as e:
                logger.debug("打开窗口 [%s] 失败: %s", chat, e)
        logger.info("已打开 %d 个独立窗口", opened)

    def _global_scan(self, Navigator, Monitor) -> None:
        from pyweixin.utils import scan_for_new_messages
        try:
            result = scan_for_new_messages(close_weixin=False)
            if not result:
                return
            for chat_name, count in result.items():
                if chat_name in self._excluded:
                    continue
                if chat_name in self._dialog_windows:
                    continue  # 已监控的不重复处理
                if count > 0:
                    logger.info("全局扫描发现: [%s] %d 条新消息", chat_name, count)
                    try:
                        dw, is_group = Navigator.open_seperate_dialog_window(
                            friend=chat_name, window_minimize=False, close_weixin=False,
                            return_is_group=True,
                        )
                        self._chat_group_status[chat_name] = is_group
                        self._read_last_message(dw, chat_name)
                        # 加入监控列表，之后的轮询会自动处理
                        self._dialog_windows[chat_name] = dw
                        logger.info("已将 [%s] 加入监控列表", chat_name)
                    except Exception as e:
                        logger.debug("全局扫描读取 [%s] 失败: %s", chat_name, e)
        except Exception as e:
            logger.debug("全局扫描异常: %s", e)
        # 扫描完后归位会话列表
        self._type_home_on_session_list()

    def _type_home_on_session_list(self):
        """将主窗口的会话列表滚回顶部"""
        try:
            import win32gui
            from pywinauto import Desktop
            from pyweixin.Uielements import Main_window as MainWindowUI
            desktop = Desktop(backend='uia')
            hwnd = win32gui.FindWindow('Qt51514QWindowIcon', '微信')
            if hwnd == 0:
                hwnd = win32gui.FindWindow('Qt51514QWindowIcon', 'Weixin')
            if hwnd:
                mw = desktop.window(handle=hwnd)
                sl = mw.child_window(**MainWindowUI.SessionList)
                if sl.exists(timeout=0.2):
                    sl.type_keys('{HOME}')
        except Exception:
            pass

    def _read_last_message(self, dw: Any, chat_name: str) -> None:
        chat_list = dw.child_window(control_type="List")
        if not chat_list.exists(timeout=0.5):
            return
        items = chat_list.children(control_type="CheckBox") or chat_list.children(control_type="ListItem")
        if not items:
            return
        # 读最后一个，可能有多条新消息
        for item in items[-3:]:
            text = item.window_text()
            if not text:
                continue
            msg_type, sender, content, is_group = self._parse_message(chat_name, text, is_group=self._chat_group_status.get(chat_name, False))
            if content:
                media_path = _find_latest_media_file() or "" if msg_type in ("emoji", "image") else ""
                self._emit(chat_name, sender, content, is_group, msg_type, media_path)

    def _open_pending_windows(self) -> None:
        for chat in self._target_chats:
            if chat in self._excluded or chat in self._dialog_windows:
                continue
            try:
                dw, is_group = self._Navigator.open_seperate_dialog_window(
                    friend=chat, window_minimize=False, close_weixin=False,
                    return_is_group=True,
                )
                self._dialog_windows[chat] = dw
                self._chat_group_status[chat] = is_group
                logger.info("动态打开窗口: %s", chat)
            except Exception as e:
                logger.debug("打开窗口 [%s] 失败: %s", chat, e)

    def _poll_all_windows(self, last_seen: dict[str, Any]) -> None:
        for chat_name, dw in list(self._dialog_windows.items()):
            try:
                self._poll_single_window(chat_name, dw, last_seen)
            except Exception as e:
                logger.warning("监听 [%s] 异常: %s", chat_name, e)

    def _poll_single_window(self, chat_name: str, dw: Any, last_seen: dict[str, Any]) -> None:
        # 正在发送中，跳过本轮避免检测到自己的消息
        with self._send_lock:
            if chat_name in self._sending:
                return

        chat_list = dw.child_window(control_type="List")
        if not chat_list.exists(timeout=0.3):
            return

        items = chat_list.children(control_type="CheckBox") or chat_list.children(control_type="ListItem")
        if not items:
            return

        last = items[-1]
        rid = last.element_info.runtime_id

        if chat_name not in last_seen:
            last_seen[chat_name] = rid
            return
        if last_seen[chat_name] == rid:
            return

        # 判断消息方向——自己的消息靠右，对方的靠左
        try:
            chat_rect = chat_list.element_info.rectangle
            item_rect = last.element_info.rectangle
            chat_center_x = chat_rect.left + (chat_rect.right - chat_rect.left) / 2
            item_center_x = item_rect.left + (item_rect.right - item_rect.left) / 2
            if item_center_x > chat_center_x:
                # 靠右 → 自己发的消息，跳过
                last_seen[chat_name] = rid
                return
        except Exception:
            pass  # 取不到位置就按原逻辑走

        text = last.window_text()
        if not text:
            last_seen[chat_name] = rid
            return

        last_seen[chat_name] = rid

        # 检测消息类型（动画表情/图片/视频/文本）
        is_group = self._chat_group_status.get(chat_name, False)
        msg_type, sender, content, is_group = self._parse_message(chat_name, text, is_group=is_group)
        if not content:
            return

        # 动画表情/图片尝试从 WeChat 文件目录找到实际文件
        media_path = ""
        if msg_type in ("emoji", "image"):
            media_path = _find_latest_media_file() or ""
            if media_path:
                logger.info("找到媒体文件: %s", media_path)

        logger.info("检测到 [%s] %s: %s (%s)", chat_name, sender, content[:60], msg_type)
        self._emit(chat_name, sender, content, is_group, msg_type, media_path)

    @staticmethod
    def _parse_message(chat_name: str, text: str, is_group: bool = False) -> tuple[str, str, str, bool]:
        # 特殊标签检测
        emoji_labels = ("动画表情", "Animated Stickers", "動態貼圖")
        image_labels = ("[图片]", "图片", "[Image]", "Image", "[圖片]", "圖片")
        video_labels = ("[视频]", "视频", "[Video]", "Video", "[影片]", "影片")

        if any(text.strip() == l for l in emoji_labels):
            return "emoji", chat_name, "[动画表情]", False

        if any(text.strip().startswith(l) for l in image_labels):
            return "image", chat_name, "[图片]", False

        if any(text.strip().startswith(l) for l in video_labels):
            return "video", chat_name, "[视频]", False

        # 已知是群聊：尝试用 "\n" 拆分出发送者
        if is_group:
            lines = text.split("\n", 1)
            if len(lines) >= 2:
                return "text", lines[0].strip(), lines[1].strip(), True
            # 独立窗口可能不显示昵称前缀，fallback 以聊天名作为发送者
            return "text", chat_name, text.strip(), True

        # 已知是私聊
        return "text", chat_name, text.strip(), False

    def _emit(self, chat: str, sender: str, content: str, is_group: bool, msg_type: str = "text", media_path: str = "") -> None:
        if _is_known(_dedup_key(chat, sender, content)):
            return
        if sender in (self._my_name, "Self", "本人(MySelf)"):
            return
        if chat in self._excluded:
            return
        msg = {
            "type": "inbound",
            "chat": chat,
            "sender": sender,
            "content": content,
            "is_group": is_group,
            "msg_type": msg_type,
            "media_path": media_path,
        }
        if self._on_message is not None:
            self._on_message(msg)
