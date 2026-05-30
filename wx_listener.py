from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("wemai_client.listener")


def _save_chat_media(chat: str, msg_type: str) -> str | None:
    """用 pyweixin 保存聊天中的最新图片到临时目录，返回文件路径"""
    global _LATEST_MEDIA_FILE
    try:
        import tempfile
        from pyweixin import Messages, GlobalConfig
        target = tempfile.mkdtemp(prefix="wemai_media_")
        GlobalConfig.close_weixin = False
        Messages.save_media(friend=chat, number=1, target_folder=target)
        images_dir = os.path.join(target, "Images")
        if os.path.isdir(images_dir):
            files = sorted(os.listdir(images_dir), key=lambda f: os.path.getmtime(os.path.join(images_dir, f)), reverse=True)
            for fname in files:
                fpath = os.path.join(images_dir, fname)
                if fpath == _LATEST_MEDIA_FILE:
                    continue
                if _detect_image_type_static(fpath):
                    _LATEST_MEDIA_FILE = fpath
                    return fpath
        return None
    except Exception as e:
        logger.warning("保存聊天媒体失败: %s", e)
        return None


def _find_latest_emoticon() -> str | None:
    """从微信缓存中查找最新收到的动画表情/GIF，返回文件路径"""
    global _LATEST_MEDIA_FILE
    try:
        from pyweixin import Tools
        wxid = Tools.where_wxid_folder()
        logger.info("表情查找: wxid=%s", wxid)
        if not wxid:
            return None
        candidates = []
        emoticon_dirs = [
            os.path.join(wxid, "FileStorage", "CustomEmotion"),
            os.path.join(wxid, "FileStorage", "Image"),
            os.path.join(wxid, "cache"),
            os.path.join(wxid, "Msg", "file"),
        ]
        now = time.time()
        for base in emoticon_dirs:
            if not os.path.isdir(base):
                logger.info("表情查找: 目录不存在 %s", base)
                continue
            logger.info("表情查找: 扫描 %s", base)
            file_count = 0
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d.lower() not in ("thumb", "temp", "head_image", "bubble", "sns", "weappicon")]
                for f in files:
                    fpath = os.path.join(root, f)
                    if fpath == _LATEST_MEDIA_FILE:
                        continue
                    try:
                        mtime = os.path.getmtime(fpath)
                        size = os.path.getsize(fpath)
                    except OSError:
                        continue
                    if size < 1024 or now - mtime > 60:
                        continue
                    ext = os.path.splitext(f)[1].lower()
                    # 有扩展名: 直接匹配图片后缀; 无扩展名: 魔数检测
                    if ext in (".gif", ".jpg", ".jpeg", ".png", ".webp", ".bmp"):
                        candidates.append((fpath, mtime))
                        file_count += 1
                    elif ext == "" and _detect_image_type_static(fpath):
                        candidates.append((fpath, mtime))
                        file_count += 1
            logger.info("表情查找: %s 找到 %d 个候选", base, file_count)
        logger.info("表情查找: 共 %d 个候选", len(candidates))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1], reverse=True)
        result = candidates[0][0]
        logger.info("表情查找: 选中 %s", result)
        _LATEST_MEDIA_FILE = result
        return result
    except Exception as e:
        logger.warning("表情查找异常: %s", e)
        return None
        # 微信 3.x: FileStorage, 微信 4.0: cache/msg attach 等
        search_dirs = [
            os.path.join(wxid_folder, "FileStorage"),
            os.path.join(wxid_folder, "msg", "file"),
            os.path.join(wxid_folder, "msg", "attach"),
            os.path.join(wxid_folder, "file"),
            os.path.join(wxid_folder, "cache"),
        ]
        candidates = []
        image_ext = (".gif", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".dat")
        for base in search_dirs:
            if not os.path.isdir(base):
                continue
            logger.info("媒体查找: 扫描目录 %s", base)
            for root, dirs, files in os.walk(base):
                for f in files:
                    fpath = os.path.join(root, f)
                    if fpath == _LATEST_MEDIA_FILE:
                        continue
                    ext = os.path.splitext(f)[1].lower()
                    # 允许已知图片扩展名 OR 无扩展名（微信 4.0 缓存文件）
                    if ext not in image_ext and ext != "":
                        continue
                    # 无扩展名或 .dat 文件需要魔数验证
                    if ext in ("", ".dat"):
                        img_type = _detect_image_type_static(fpath)
                        if img_type is None:
                            continue
                    try:
                        mtime = os.path.getmtime(fpath)
                        size = os.path.getsize(fpath)
                    except OSError:
                        continue
                    if size < 1024:  # 头像缩略图等小文件跳过
                        continue
                    candidates.append((fpath, mtime))
        logger.info("媒体查找: %d 候选", len(candidates))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1], reverse=True)
        result = candidates[0][0]
        logger.info("媒体查找: 选中 %s", result)
        _LATEST_MEDIA_FILE = result
        return result
    except Exception as e:
        logger.warning("媒体查找异常: %s", e)
        return None


_KNOWN_IDS: set[str] = set()
_KNOWN_LOCK = threading.Lock()

# 最近一次已发送的媒体文件路径，防止重复拿同一个文件
_LATEST_MEDIA_FILE: str = ""


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


def _detect_image_type_static(path: str) -> str | None:
    """从文件头检测真实图片类型，不依赖扩展名"""
    try:
        with open(path, "rb") as f:
            header = f.read(8)
        if header.startswith(b'\x89PNG\r\n\x1a\n'):
            return '.png'
        if header.startswith(b'\xff\xd8'):
            return '.jpg'
        if header.startswith(b'GIF87a') or header.startswith(b'GIF89a'):
            return '.gif'
        if header.startswith(b'RIFF'):
            return '.webp'
        return None
    except Exception:
        return None


class WeChatListener:
    def __init__(
        self,
        target_chats: list[str],
        excluded: list[str],
        poll_interval: float,
        close_weixin: bool,
        send_delay: float,
        on_message: Optional[Callable[[dict], None]] = None,
        group_members: Optional[dict[str, list[str]]] = None,
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
        # 群成员列表，key=聊天名称 value=成员昵称列表
        self._chat_group_members: dict[str, list[str]] = group_members or {}
        # Adapter 明确告知的群聊名称集合（比 pyweixin 自动检测更可靠）
        self._adapter_group_list: set[str] = set()
        # 每个聊天上一次实际发出的消息 (sender, content)，用于拦截撤回导致的 runtime_id 漂移
        self._last_msg: dict[str, tuple[str, str]] = {}

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
        # 记录 Adapter 明确告知的群聊
        self._adapter_group_list.update(group_list)
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

        try:
            Navigator.open_weixin()
            logger.info("微信主窗口已打开")
        except Exception as e:
            logger.error("打开微信失败: %s", e)
            return

        # 拉大微信主窗口，让独立窗口有足够空间
        try:
            import win32gui
            hwnd = win32gui.FindWindow('Qt51514QWindowIcon', '微信')
            if hwnd == 0:
                hwnd = win32gui.FindWindow('Qt51514QWindowIcon', 'Weixin')
            if hwnd:
                win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
                time.sleep(0.2)
                rect = win32gui.GetWindowRect(hwnd)
                w, h = rect[2] - rect[0], rect[3] - rect[1]
                target_w, target_h = max(w, 1200), max(h, 800)
                if w < target_w or h < target_h:
                    win32gui.MoveWindow(hwnd, rect[0], rect[1], target_w, target_h, True)
                # 直接写死目标尺寸，避免 pyweixin 内部缩回去
                GlobalConfig.window_size = (target_w, target_h)
                logger.info("微信主窗口已调整: %dx%d → %dx%d", w, h, target_w, target_h)
        except Exception as e:
            logger.warning("调整微信窗口大小失败: %s", e)

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
                # 优先使用 Adapter 的群聊信息（比 pyweixin 自动检测更准确）
                if chat in self._adapter_group_list:
                    is_group = True
                self._dialog_windows[chat] = dw
                self._chat_group_status[chat] = is_group
                opened += 1
                if opened % 5 == 0:
                    time.sleep(0.5)
            except Exception as e:
                logger.debug("打开窗口 [%s] 失败: %s", chat, e)
        # 群聊独立窗口激活多选模式，私聊跳过
        if self._dialog_windows:
            logger.info("在群聊独立窗口上激活多选模式...")
        for chat, dw in self._dialog_windows.items():
            if chat in self._excluded:
                continue
            if not self._chat_group_status.get(chat, False):
                continue  # 私聊不需要多选
            # 重试直到成功，确保多选打开后再开始轮询
            for retry in range(5):
                if self._activate_multiselect(chat, dw):
                    break
                logger.warning("多选激活重试 %d/5 [%s]", retry + 1, chat)
                time.sleep(1.5)
            time.sleep(0.3)  # 窗口间留间隔
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
                        if chat_name in self._adapter_group_list:
                            is_group = True
                        self._chat_group_status[chat_name] = is_group
                        self._dialog_windows[chat_name] = dw
                        logger.info("已将 [%s] 加入监控列表", chat_name)
                        # 群聊则激活多选
                        if is_group:
                            for retry in range(5):
                                try:
                                    time.sleep(0.3)
                                    if self._activate_multiselect(chat_name, dw):
                                        break
                                except Exception:
                                    pass
                                time.sleep(1.5)
                        # 读取最后几条消息
                        self._read_last_message(dw, chat_name)
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
        # 多选模式下使用 CheckBox，其文本为 "发送人 内容"
        cbs = chat_list.children(control_type="CheckBox")
        items = cbs or chat_list.children(control_type="ListItem")
        if not items:
            return
        is_multiselect = len(cbs) > 0
        # 读最后 N 条，预载入去重缓存，防止撤回后漂移
        for item in items[-10:]:
            text = item.window_text()
            if not text:
                continue
            is_group = self._chat_group_status.get(chat_name, False)
            if is_multiselect:
                msg_type, sender, content = self._parse_multiselect_text(chat_name, text, is_group)
                if not sender:
                    continue  # 自己的消息，跳过
            else:
                msg_type, sender, content, is_group = self._parse_message(chat_name, text, is_group=is_group)
            if content:
                if msg_type == "image":
                    media_path = _save_chat_media(chat_name, msg_type) or ""
                elif msg_type == "emoji":
                    media_path = _find_latest_emoticon() or ""
                else:
                    media_path = ""
                media_base64 = self._file_to_base64(media_path) if media_path else ""
                detected = self._detect_image_type(media_path) if media_path else None
                media_ext = detected or os.path.splitext(media_path)[1] or ".png" if media_path else ""
                self._emit(chat_name, sender, content, is_group, msg_type, media_path, media_base64, media_ext)

    def _open_pending_windows(self) -> None:
        for chat in self._target_chats:
            if chat in self._excluded or chat in self._dialog_windows:
                continue
            try:
                dw, is_group = self._Navigator.open_seperate_dialog_window(
                    friend=chat, window_minimize=False, close_weixin=False,
                    return_is_group=True,
                )
                if chat in self._adapter_group_list:
                    is_group = True
                self._dialog_windows[chat] = dw
                self._chat_group_status[chat] = is_group
                logger.info("动态打开窗口: %s", chat)
                # 群聊则激活多选模式
                if is_group:
                    for retry in range(5):
                        time.sleep(0.3)
                        if self._activate_multiselect(chat, dw):
                            break
                        time.sleep(1.5)
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

        # 多选模式下用 CheckBox，格式 "发送人 消息内容"
        cbs = chat_list.children(control_type="CheckBox")
        is_multiselect = len(cbs) > 0

        items = cbs if is_multiselect else (chat_list.children(control_type="ListItem") or [])
        if not items:
            return

        last = items[-1]
        rid = last.element_info.runtime_id

        if chat_name not in last_seen:
            last_seen[chat_name] = rid
            return
        if last_seen[chat_name] == rid:
            return

        text = last.window_text()
        if not text:
            last_seen[chat_name] = rid
            return

        last_seen[chat_name] = rid

        is_group = self._chat_group_status.get(chat_name, False)

        if is_multiselect:
            # 多选模式：用群成员正则匹配发送人
            msg_type, sender, content = self._parse_multiselect_text(chat_name, text, is_group)
            if not sender:
                return  # 自己的消息，跳过
        else:
            # 多选未激活：趁新消息来了，再试一次激活
            if is_group:
                self._activate_multiselect(chat_name, dw)
                # 激活成功则等下一轮轮询用多选路径处理
                if chat_list.children(control_type="CheckBox"):
                    return
            # 非多选模式（回退）：气泡位置判断方向
            try:
                chat_rect = chat_list.element_info.rectangle
                item_rect = last.element_info.rectangle
                chat_center_x = chat_rect.left + (chat_rect.right - chat_rect.left) / 2
                item_center_x = item_rect.left + (item_rect.right - item_rect.left) / 2
                if item_center_x > chat_center_x:
                    last_seen[chat_name] = rid
                    return
            except Exception:
                pass
            msg_type, sender, content, is_group = self._parse_message(chat_name, text, is_group=is_group)
            if not content:
                return

        # 撤回等操作会导致 runtime_id 漂移，但内容相同则跳过
        cur = (sender, content)
        if self._last_msg.get(chat_name) == cur:
            logger.debug("内容重复，跳过 (chat=%s sender=%s)", chat_name, sender)
            return
        self._last_msg[chat_name] = cur

        # 动画表情/图片尝试从 WeChat 文件目录找到实际文件
        media_path = ""
        media_base64 = ""
        media_ext = ""
        if msg_type == "image":
            media_path = _save_chat_media(chat_name, msg_type) or ""
        elif msg_type == "emoji":
            media_path = _find_latest_emoticon() or ""
            if media_path:
                logger.info("找到媒体文件: %s", media_path)
                # .dat 文件从头部检测真实类型
                detected = self._detect_image_type(media_path)
                media_ext = detected or os.path.splitext(media_path)[1] or ".png"
                media_base64 = self._file_to_base64(media_path)

        logger.info("检测到 [%s] %s: %s (%s)", chat_name, sender, content[:60], msg_type)
        self._emit(chat_name, sender, content, is_group, msg_type, media_path, media_base64, media_ext)

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

    def _activate_multiselect(self, chat_name: str, dw) -> bool:
        """对独立窗口激活多选模式。成功后 CheckBox.window_text() 变为 '发送人 消息内容'。"""
        try:
            from pywinauto import mouse, Desktop

            # 只对群聊激活
            if not self._chat_group_status.get(chat_name, False):
                return False

            # 如果已存在 CheckBox，说明多选模式已经激活，跳过
            try:
                existing = dw.child_window(control_type="List").children(control_type="CheckBox")
                if existing:
                    logger.info("多选模式已存在: [%s]", chat_name)
                    return True
            except Exception:
                pass

            # 强制窗口前台+还原
            try:
                dw.set_focus()
                time.sleep(0.2)
                dw.restore()
                time.sleep(0.3)
            except Exception as e:
                logger.warning("激活多选 前置焦点 [%s]: %s", chat_name, e)

            chat_list = dw.child_window(control_type="List")
            if not chat_list.exists(timeout=0.5):
                logger.warning("激活多选 找不到 List [%s]", chat_name)
                return False

            # 激活列表
            try:
                pos = (chat_list.rectangle().right - 12, chat_list.rectangle().mid_point().y)
                mouse.click(coords=pos)
                chat_list.type_keys("{END}")
            except Exception as e:
                logger.warning("激活多选 激活列表失败 [%s]: %s", chat_name, e)
                return False

            # 逐条往上找非系统消息
            all_items = chat_list.children(control_type="ListItem")
            if not all_items:
                logger.warning("激活多选 List 无子项 [%s]", chat_name)
                return False

            for li in reversed(all_items):
                if li.class_name() == "mmui::ChatItemView":
                    continue  # 系统消息不可选
                # 选中它
                try:
                    li.click_input()
                except Exception:
                    continue
                rect = li.rectangle()
                logger.info(
                    "激活多选 找到可选项 class=%s rect=%s [%s], 尝试右键...",
                    li.class_name(), rect, chat_name,
                )

                # 用三个位置试右键
                for click_pos in [
                    (rect.left + 120, rect.mid_point().y),
                    (rect.right - 120, rect.mid_point().y),
                    (rect.mid_point().x, rect.mid_point().y),
                ]:
                    mouse.right_click(coords=click_pos)
                    time.sleep(0.3)

                    item = None
                    # 在窗口内找
                    item = dw.child_window(title="多选", control_type="MenuItem")
                    # 窗口内找不到，搜 Desktop 下的弹出菜单
                    if not item or not item.exists(timeout=0.2):
                        try:
                            desk = Desktop(backend='uia')
                            for ctrl_type in ('Menu', 'Popup'):
                                popup = desk.window(control_type=ctrl_type, found_index=0)
                                if popup.exists(timeout=0.1):
                                    item = popup.child_window(title="多选", control_type="MenuItem")
                                    if item.exists(timeout=0.1):
                                        break
                        except Exception:
                            pass

                    if item is not None and item.exists(timeout=0.3):
                        item.click_input()
                        time.sleep(0.1)
                        mouse.click(coords=click_pos)  # 关闭菜单
                        logger.info("多选模式已激活: [%s]", chat_name)
                        # 预加载去重缓存，防止撤回导致 items[-1] 漂移到旧消息
                        self._seed_dedup_cache(chat_name, chat_list)
                        return True

                logger.warning(
                    "激活多选 右键后未找到'多选'菜单项 [%s] (last_pos=%s)",
                    chat_name, click_pos,
                )
                break  # 找到可选项但激活失败，放弃
        except Exception as e:
            logger.warning("激活多选 异常 [%s]: %s", chat_name, e)
        return False

    def _seed_dedup_cache(self, chat_name: str, chat_list) -> None:
        """预加载最近消息的去重缓存，防止撤回后 runtime_id 漂移导致旧消息被重发。"""
        try:
            time.sleep(0.3)
            cbs = chat_list.children(control_type="CheckBox")
            count = 0
            for cb in reversed(cbs):
                text = cb.window_text()
                if not text:
                    continue
                _, sender, content = self._parse_multiselect_text(chat_name, text, True)
                if not sender:
                    continue
                _is_known(_dedup_key(chat_name, sender, content))
                count += 1
                if count >= 20:
                    break
            if count:
                logger.info("已为 [%s] 预加载 %d 条去重缓存", chat_name, count)
        except Exception as e:
            logger.debug("预加载去重缓存失败 [%s]: %s", chat_name, e)

    def _parse_multiselect_text(self, chat_name: str, raw_text: str, is_group: bool) -> tuple[str, str, str]:
        """
        解析多选模式下 CheckBox.window_text()，格式为 "发送人 消息内容"。
        返回 (msg_type, sender, content)。
        如遇到自己的消息，返回 ("", "", "") 供调用方跳过。
        """
        msg_type = "text"
        text = raw_text.strip()
        sender = ""
        content = text

        emoji_labels = ("动画表情", "Animated Stickers", "動態貼圖")
        image_labels = ("[图片]", "图片", "[Image]", "Image", "[圖片]", "圖片")
        video_labels = ("[视频]", "视频", "[Video]", "Video", "[影片]", "影片")

        if is_group:
            # 群聊：用群成员列表正则匹配发送人
            group_members = self._chat_group_members.get(chat_name, [])
            for gm in group_members:
                if re.match(rf"^{re.escape(gm)}\s", text):
                    sender = gm
                    content = text[len(sender) + 1:].strip()
                    break
            else:
                # 匹配不到已知成员
                if self._my_name and text.startswith(self._my_name + " "):
                    return "", "", ""  # 自己的消息
                # fallback：第一个词作为发送者
                parts = text.split(" ", 1)
                if len(parts) >= 2:
                    sender = parts[0]
                    content = parts[1].strip()
                else:
                    sender = chat_name
                    content = text
        else:
            # 私聊：文本可能为 "对方名 内容" 或仅 "内容"
            if text.startswith(chat_name + " "):
                sender = chat_name
                content = text[len(chat_name) + 1:].strip()
            elif self._my_name and text.startswith(self._my_name + " "):
                return "", "", ""
            else:
                sender = chat_name
                content = text

        # 从 content 检测特殊消息类型
        ct = content.strip()
        if any(ct == l for l in emoji_labels):
            msg_type = "emoji"
        elif any(ct.startswith(l) for l in image_labels):
            msg_type = "image"
        elif any(ct.startswith(l) for l in video_labels):
            msg_type = "video"

        return msg_type, sender, content

    @staticmethod
    def _detect_image_type(path: str) -> str | None:
        """从文件头部检测真实图片类型（兼容可能被重命名的 .dat 文件）"""
        try:
            with open(path, "rb") as f:
                header = f.read(8)
            # 常见图片魔数
            if header.startswith(b'\x89PNG\r\n\x1a\n'):
                return '.png'
            if header.startswith(b'\xff\xd8'):
                return '.jpg'
            if header.startswith(b'GIF87a') or header.startswith(b'GIF89a'):
                return '.gif'
            if header.startswith(b'RIFF'):
                return '.webp'
            return None
        except Exception:
            return None

    @staticmethod
    def _file_to_base64(path: str, max_size: int = 5 * 1024 * 1024) -> str:
        """读取图片文件并返回 base64 字符串（超过 max_size 返回空）"""
        try:
            size = os.path.getsize(path)
            if size > max_size or size == 0:
                return ""
            import base64
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        except Exception:
            return ""

    def _emit(self, chat: str, sender: str, content: str, is_group: bool, msg_type: str = "text", media_path: str = "", media_base64: str = "", media_ext: str = "") -> None:
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
            "media_base64": media_base64,
            "media_ext": media_ext,
        }
        if self._on_message is not None:
            self._on_message(msg)
