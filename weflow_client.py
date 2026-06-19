from __future__ import annotations

import base64 as _b64
import json
import logging
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import requests

logger = logging.getLogger("wemai_client.weflow")

_LOCAL_TYPE_MAP: dict[int, str] = {
    1: "text", 3: "image", 34: "voice", 43: "video", 47: "emoji", 49: "text", 10000: "",
}

_MEDIA_CONTENT_PREFIXES = ("[图片]", "[Image]", "[圖片]", "图片",
                           "[视频]", "[Video]", "[影片]", "视频",
                           "动画表情", "Animated Stickers", "動態貼圖")


def _is_media_content(content: str) -> bool:
    return any(content.startswith(p) for p in _MEDIA_CONTENT_PREFIXES)


class WeFlowClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:5031",
        api_token: str = "",
    ) -> None:
        self._base = base_url.rstrip("/")
        self._api_token = api_token

        self._session = requests.Session()
        if api_token:
            self._session.headers["Authorization"] = f"Bearer {api_token}"

        self._running = False
        self._sse_thread: threading.Thread | None = None
        self._on_message: Callable[[dict], None] | None = None
        self._on_revoke: Callable[[dict], None] | None = None

        self._first_connect_ts: float = 0.0
        self._seen_fp: set[str] = set()

        self._name_to_wxid: dict[str, str] = {}
        self._wxid_to_name: dict[str, str] = {}
        self._wxid_to_type: dict[str, str] = {}
        self._group_members: dict[str, dict[str, str]] = {}

    # ─── public API ───────────────────────────────────────────

    def set_handlers(
        self,
        on_message: Callable[[dict], None],
        on_revoke: Callable[[dict], None] | None = None,
    ) -> None:
        self._on_message = on_message
        self._on_revoke = on_revoke

    def health_check(self) -> bool:
        try:
            data = self._do_get(f"{self._base}/api/v1/health", timeout=5)
            if data:
                return (
                    data.get("ok") is True
                    or data.get("status") == "ok"
                    or isinstance(data.get("data"), dict) and data["data"].get("ok") is True
                )
        except Exception:
            pass
        return False

    def start_push(self) -> None:
        if self._running:
            return
        self._running = True
        self._first_connect_ts = time.time()
        self._sse_thread = threading.Thread(
            target=self._sse_loop, daemon=True, name="weflow-sse"
        )
        self._sse_thread.start()
        logger.info("WeFlow SSE 线程已启动")

    def stop_push(self) -> None:
        self._running = False
        if self._sse_thread is not None:
            self._sse_thread.join(timeout=5)

    # ─── data queries ─────────────────────────────────────────

    def get_sessions(self, keyword: str = "", limit: int = 200) -> list[dict]:
        params = {"limit": str(limit)}
        if keyword:
            params["keyword"] = keyword
        return self._unwrap_data(self._get("/api/v1/sessions", params))

    def get_messages(
        self, session_id: str, limit: int = 100, offset: int = 0,
        start: int | None = None, media: bool = False,
    ) -> list[dict]:
        params: dict[str, str] = {"limit": str(limit), "offset": str(offset), "talker": session_id}
        if start is not None:
            params["start"] = str(start)
        if media:
            params["media"] = "1"
        return self._unwrap_data(self._get("/api/v1/messages", params))

    def get_contacts(self, keyword: str = "", limit: int = 500) -> list[dict]:
        params = {"limit": str(limit)}
        if keyword:
            params["keyword"] = keyword
        return self._unwrap_data(self._get("/api/v1/contacts", params))

    def get_group_members(self, chatroom_id: str) -> list[dict]:
        resp = self._get("/api/v1/group-members", {"chatroomId": chatroom_id})
        members = resp.get("members")
        if isinstance(members, list):
            return members
        resp = self._get(f"/api/v1/groups/{chatroom_id}/members")
        return self._unwrap_data(resp)

    def get_moments(self, limit: int = 20, **kw) -> list[dict]:
        params: dict[str, str] = {"limit": str(limit), "media": "1"}
        for k in ("keyword", "usernames", "start", "end", "offset"):
            v = kw.get(k)
            if v:
                params[k] = str(v)
        return self._unwrap_data(self._get("/api/v1/sns/timeline", params))

    # ─── mapping tables ───────────────────────────────────────

    def build_mapping(self) -> None:
        new_n2w: dict[str, str] = {}
        new_w2n: dict[str, str] = {}
        wxid_type: dict[str, str] = {}

        try:
            for s in self.get_sessions():
                wxid = s.get("session_id") or s.get("sessionId") or s.get("username") or s.get("userName") or s.get("talker") or ""
                if not wxid:
                    continue
                display = s.get("displayName") or s.get("display_name") or s.get("name") or s.get("nickname") or s.get("nickName") or wxid
                st = s.get("sessionType") or s.get("type", "")
                t = "group" if (st in (2, "2", "group") or str(st).endswith("chatroom")) else "private"
                wxid_type[wxid] = t
                if display and display != wxid:
                    new_n2w[display] = wxid
                new_w2n[wxid] = display
        except Exception as e:
            logger.warning("build sessions %s", e)

        try:
            for c in self.get_contacts():
                wxid = c.get("username") or c.get("userName") or c.get("wxid") or ""
                if not wxid:
                    continue
                display = c.get("displayName") or c.get("display_name") or c.get("nickname") or c.get("nickName") or c.get("remark") or wxid
                if display and display != wxid:
                    new_n2w[display] = wxid
                if wxid not in new_w2n:
                    new_w2n[wxid] = display
                if wxid not in wxid_type:
                    wxid_type[wxid] = "private"
        except Exception as e:
            logger.warning("build contacts %s", e)

        try:
            for gid in [wid for wid, t in wxid_type.items() if t == "group"]:
                try:
                    gm: dict[str, str] = {}
                    for m in self.get_group_members(gid):
                        m_wxid = m.get("wxid") or m.get("username") or m.get("userName") or ""
                        m_name = m.get("displayName") or m.get("display_name") or m.get("nickname") or m.get("nickName") or m.get("groupNickname") or m.get("group_nickname") or m_wxid
                        if m_wxid:
                            gm[m_wxid] = m_name
                            if m_name and m_name != m_wxid:
                                new_n2w[m_name] = m_wxid
                            if m_wxid not in new_w2n:
                                new_w2n[m_wxid] = m_name
                    self._group_members[gid] = gm
                except Exception:
                    pass
        except Exception as e:
            logger.warning("build groups %s", e)

        self._name_to_wxid = new_n2w
        self._wxid_to_name = new_w2n
        self._wxid_to_type = wxid_type
        logger.info("映射表: %d names, %d wxids, %d group sessions",
                     len(self._name_to_wxid), len(self._wxid_to_name),
                     sum(1 for t in wxid_type.values() if t == "group"))

    def resolve_name(self, display_name: str) -> str:
        return self._name_to_wxid.get(display_name, display_name)

    def lookup_name(self, wxid: str) -> str:
        return self._wxid_to_name.get(wxid, wxid)

    def session_type(self, wxid: str) -> str:
        return self._wxid_to_type.get(wxid, "private")

    def member_name(self, chatroom_id: str, wxid: str) -> str:
        return self._group_members.get(chatroom_id, {}).get(wxid, self.lookup_name(wxid))

    # ─── XML parsing ──────────────────────────────────────────

    @staticmethod
    def _parse_appmsg(root: ET.Element) -> dict:
        appmsg = root.find(".//appmsg")
        if appmsg is None:
            return {}

        def _txt(tag: str) -> str:
            el = appmsg.find(tag)
            return el.text.strip() if el is not None and el.text else ""

        def _txt_or_none(tag: str) -> str | None:
            el = appmsg.find(tag)
            return el.text.strip() if el is not None and el.text else None

        info = {
            "app_type": _txt("type"),
            "title": _txt("title"),
            "description": _txt("des"),
            "url": _txt_or_none("url"),
        }

        appinfo = root.find(".//appinfo")
        if appinfo is not None:
            appname_el = appinfo.find("appname")
            if appname_el is not None and appname_el.text:
                info["app_name"] = appname_el.text.strip()

        return info

    @staticmethod
    def _parse_revoke(root: ET.Element) -> dict | None:
        revoke = root.find(".//revokemsg")
        if revoke is not None and revoke.text:
            return {"revoke_old_content": revoke.text.strip()}
        return None

    @staticmethod
    def _parse_reply(root: ET.Element) -> dict | None:
        reply = root.find(".//reply")
        if reply is None:
            return None
        r = {}
        for child in reply:
            r[child.tag] = child.text.strip() if child.text else ""
        return r

    def _parse_raw_xml(self, raw_xml: str) -> dict:
        if not raw_xml or not raw_xml.strip().startswith("<"):
            return {}

        try:
            cleaned = raw_xml.replace('<?xml version="1.0"?>', "").strip()
            root = ET.fromstring(cleaned)
        except ET.ParseError as e:
            logger.debug("XML 解析失败: %s", e)
            return {"_parse_error": str(e)}

        result = {}

        fu = root.find(".//fromusername")
        if fu is not None and fu.text:
            result["from_username"] = fu.text.strip()

        app = self._parse_appmsg(root)
        if app:
            result["appmsg"] = app

        rev = self._parse_revoke(root)
        if rev:
            result["revoke"] = rev

        reply = self._parse_reply(root)
        if reply:
            result["reply"] = reply

        return result

    # ─── SSE loop ─────────────────────────────────────────────

    def _sse_loop(self) -> None:
        electron_url = f"{self._base}/api/v1/push/messages"
        if self._api_token:
            electron_url += f"?access_token={self._api_token}"
        rust_url = f"{self._base}/api/v1/events"

        while self._running:
            try:
                resp = self._session.get(electron_url, stream=True, timeout=(10, None))
                resp.raise_for_status()
                logger.info("SSE 已连接 (Electron)")
                self._sse_read_electron(resp)
                logger.info("SSE 断开，准备重连")
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                logger.warning("SSE Electron 连接失败: %s", e)
                try:
                    resp = self._session.get(rust_url, stream=True, timeout=(10, None))
                    resp.raise_for_status()
                    logger.info("SSE 已连接 (Rust CLI)")
                    self._sse_read_rust(resp)
                    logger.info("SSE 断开，准备重连")
                except Exception as e2:
                    logger.warning("SSE Rust CLI 也失败: %s", e2)

            if not self._running:
                break
            time.sleep(3)

    def _sse_read_electron(self, resp: requests.Response) -> None:
        current: dict[str, str] = {}
        for line in resp.iter_lines(decode_unicode=True):
            if not self._running:
                return
            if line is None:
                continue
            if line == "":
                if "data" in current:
                    self._dispatch_event(current)
                current = {}
                continue
            if line.startswith("event:"):
                current["event"] = line[6:].strip()
            elif line.startswith("data:"):
                current["data"] = line[5:].strip()
            elif line.startswith("id:"):
                current["id"] = line[3:].strip()

    def _sse_read_rust(self, resp: requests.Response) -> None:
        for line in resp.iter_lines(decode_unicode=True):
            if not self._running:
                return
            if line is None or not line.startswith("data: "):
                continue
            data_str = line[6:]
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            self._handle_rust_sse(event)

    def _dispatch_event(self, current: dict[str, str]) -> None:
        ev = current.get("event", "")
        data_raw = current.get("data", "")
        if not data_raw:
            return
        try:
            data = json.loads(data_raw)
        except json.JSONDecodeError:
            logger.debug("SSE JSON 解析失败: %s", data_raw[:100])
            return
        if ev == "message.new":
            self._handle_message(data)
        elif ev == "message.revoke":
            if data.get("rawid") and self._on_revoke:
                self._on_revoke(data)

    # ─── message handling ──────────────────────────────────────

    def _handle_message(self, data: dict) -> None:
        rawid = str(data.get("rawid", ""))
        if not rawid:
            return

        ts = data.get("timestamp", 0)
        if self._first_connect_ts and ts < self._first_connect_ts - 1:
            return

        if rawid in self._seen_fp:
            logger.debug("跳过已处理消息: %s", rawid)
            return
        self._seen_fp.add(rawid)
        if len(self._seen_fp) > 50000:
            self._seen_fp.clear()

        sid = data.get("sessionId", "")
        content = data.get("content", "")
        source_name = data.get("sourceName", "")
        group_name = data.get("groupName", "")
        stype = data.get("sessionType", "")
        is_group = stype == "group"
        chat_name = group_name or sid
        sender_name = source_name
        sender_wxid = self._name_to_wxid.get(sender_name, sender_name)
        chat_wxid = self._name_to_wxid.get(chat_name) or sid

        msg = {
            "type": "inbound",
            "chat": chat_wxid, "chat_name": chat_name,
            "sender": sender_wxid, "sender_name": sender_name,
            "content": content, "is_group": is_group,
            "msg_type": "text",
            "media_path": "", "media_base64": "", "media_ext": "",
            "media_url": "", "server_id": rawid, "source": "weflow",
            "_detail": None,
        }

        media_base64 = ""
        media_ext = ""
        detail = None
        parsed_raw = {}

        if rawid:
            detail = self._fetch_message_detail(sid, rawid)

        if detail:
            msg["_detail"] = detail

            raw_xml = detail.get("rawContent") or ""
            parsed_raw = self._parse_raw_xml(raw_xml)

            appmsg = parsed_raw.get("appmsg")
            if appmsg:
                atype = appmsg.get("app_type", "")
                type_labels = {
                    "5": "公众号文章", "4": "链接分享",
                    "3": "图片", "6": "文件", "33": "小程序",
                }
                msg["appmsg_type"] = atype
                msg["appmsg_label"] = type_labels.get(atype, f"卡片(type={atype})")
                if appmsg.get("title"):
                    msg["appmsg_title"] = appmsg["title"]
                if appmsg.get("url"):
                    msg["appmsg_url"] = appmsg["url"]
                if appmsg.get("app_name"):
                    msg["appmsg_app_name"] = appmsg["app_name"]
                if appmsg.get("description"):
                    msg["appmsg_description"] = appmsg["description"]

            if parsed_raw.get("reply"):
                msg["reply"] = parsed_raw["reply"]

            if parsed_raw.get("revoke"):
                msg["revoke"] = parsed_raw["revoke"]

            mu = detail.get("mediaUrl") or ""
            mt = detail.get("mediaType") or ""
            if mu:
                media_base64, media_ext = self._download_media(mu)
                msg["media_base64"] = media_base64
                msg["media_ext"] = media_ext
                msg["media_url"] = mu
            if mt:
                msg["media_type"] = mt
                if mt == "image":
                    msg["msg_type"] = "image"
                elif mt == "emoji":
                    msg["msg_type"] = "emoji"
                elif mt == "video":
                    msg["msg_type"] = "video"
                elif mt == "voice":
                    msg["msg_type"] = "voice"

        logger.info(
            "[%s] %s%s: %s",
            "群" if is_group else "私",
            chat_name,
            f" ({parsed_raw.get('appmsg', {}).get('app_type', '')})" if parsed_raw.get("appmsg") else "",
            content[:80],
        )

        if self._on_message is not None:
            self._on_message(msg)

    # ─── rust SSE (placeholder) ───────────────────────────────

    def _handle_rust_sse(self, event: dict) -> None:
        pass

    # ─── media fetching ────────────────────────────────────────

    def _fetch_message_detail(self, session_id: str, rawid: str) -> dict | None:
        try:
            resp = self._get(
                "/api/v1/messages",
                {"talker": session_id, "limit": 50, "media": 1, "image": 1, "emoji": 1},
                timeout=10,
            )
            if not resp.get("success"):
                return None
            for m in resp.get("messages", []):
                mid = str(m.get("serverId") or m.get("localId") or "")
                if mid == rawid:
                    return m
        except Exception as e:
            logger.debug("fetch_message_detail 失败: %s", e)
        return None

    def _download_media(self, media_url: str) -> tuple[str, str]:
        url = urljoin(self._base, media_url) if media_url.startswith("/") else media_url
        t0 = time.time()
        try:
            resp = self._session.get(url, timeout=10)
            resp.raise_for_status()
            raw = resp.content
        except Exception as e:
            logger.debug("媒体下载失败: %s", e)
            return "", ""

        ext = ".png"
        ct = resp.headers.get("Content-Type", "")
        if "jpeg" in ct or "jpg" in ct or url.lower().endswith((".jpg", ".jpeg")):
            ext = ".jpg"
        elif "gif" in ct or url.lower().endswith(".gif"):
            ext = ".gif"
        elif "webp" in ct or url.lower().endswith(".webp"):
            ext = ".webp"
        b64 = _b64.b64encode(raw).decode("ascii")
        logger.info("媒体下载: %s (%d bytes, %.0fms)", ext, len(raw), (time.time() - t0) * 1000)
        return b64, ext

    # ─── HTTP helpers ─────────────────────────────────────────

    def _get(
        self, path: str,
        params: dict[str, str] | None = None,
        timeout: int = 15,
    ) -> dict:
        base = self._base.rstrip("/")
        url = f"{base}{path}" if path.startswith("/") else f"{base}/{path}"
        try:
            resp = self._session.get(url, params=params, timeout=timeout)
            if resp.status_code != 200:
                logger.debug("HTTP %s %s: %s", resp.status_code, url, resp.text[:100])
                return {}
            return resp.json() if resp.text else {}
        except Exception as e:
            logger.debug("HTTP GET %s 失败: %s", url, e)
            return {}

    def _do_get(
        self, url: str,
        params: dict[str, str] | None = None,
        timeout: int = 15,
    ) -> dict | None:
        """Raw HTTP GET returning dict or None — used by health_check which handles non-200 gracefully."""
        try:
            resp = self._session.get(url, params=params, timeout=timeout)
            return resp.json() if resp.text else {}
        except Exception as e:
            logger.debug("HTTP GET %s 失败: %s", url, e)
            return None

    @staticmethod
    def _unwrap_data(resp: dict) -> list[dict]:
        data = resp.get("data")
        if isinstance(data, list):
            return data
        if isinstance(resp, list):
            return resp
        for key in ("messages", "sessions", "contacts", "members", "timeline", "moments"):
            val = resp.get(key)
            if isinstance(val, list):
                return val
        return []
