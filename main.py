from __future__ import annotations

import asyncio
import logging
import os
import queue
import signal
import sys
import threading


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as cfg_mod
from ws_client import WsPluginClient
from wx_listener import WeChatListener
from wx_sender import WeChatSender
from wx_moments import WeChatMoments
from weflow_client import WeFlowClient

logger = logging.getLogger("wemai_client.main")
_stop_event = threading.Event()


def setup_logging(log_cfg: cfg_mod.LogConfig) -> None:
    os.makedirs(os.path.dirname(log_cfg.file) or ".", exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, log_cfg.level.upper(), logging.INFO),
        format=log_cfg.format,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_cfg.file, encoding="utf-8"),
        ],
    )


def handle_signal(sig, frame) -> None:
    logger.info("收到信号 %s", sig)
    _stop_event.set()


class WeMaiClient:
    def __init__(self, cfg: cfg_mod.ClientConfig) -> None:
        self._cfg = cfg
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws = WsPluginClient(
            host=cfg.connection.server_host,
            port=cfg.connection.server_port,
            reconnect_delay=cfg.connection.reconnect_delay,
        )
        self._outbound_queue: queue.Queue = queue.Queue()
        self._sender = WeChatSender(
            outbound_queue=self._outbound_queue,
            send_delay=0.2,
            close_weixin=False,
        )
        self._listener: WeChatListener | None = None
        self._weflow: WeFlowClient | None = None
        self._current_mode: str | None = None
        self._config_received: bool = False

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        logger.info("=" * 50)
        logger.info("WeMai Client - 微信 × MaiBot 桥接客户端")
        logger.info("服务器: %s:%s", self._cfg.connection.server_host, self._cfg.connection.server_port)
        logger.info("=" * 50)

        self._ws.set_outbound_handler(self._on_plugin_outbound)
        self._ws.set_config_handler(self._on_config_update)

        ws_task = asyncio.create_task(self._ws.run())
        logger.info("等待 Adapter 下发配置...")

        try:
            while not _stop_event.is_set():
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            self._stop_data_source()
            self._sender.stop()
            await self._ws.stop()
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
            logger.info("所有组件已停止")

    def _on_config_update(self, msg: dict) -> None:
        if self._loop is not None and self._loop.is_running():
            asyncio.ensure_future(self._apply_config_from_adaptor(msg))

    async def _apply_config_from_adaptor(self, msg: dict) -> None:
        data_source = msg.get("data_source", "pyweixin")
        admin = msg.get("admin", [])

        send_delay = msg.get("send_delay")
        close_weixin = msg.get("close_weixin")
        if send_delay is not None or close_weixin is not None:
            self._sender.update_params(send_delay=send_delay, close_weixin=close_weixin)

        if not self._config_received:
            if data_source == "weflow":
                await self._start_weflow(msg)
            else:
                await self._start_pyweixin(msg)
            self._config_received = True
            return

        if data_source != self._current_mode:
            logger.info("数据源模式切换: %s -> %s", self._current_mode, data_source)
            await self._stop_data_source()
            if data_source == "weflow":
                await self._start_weflow(msg)
            else:
                await self._start_pyweixin(msg)
            return

        if self._listener is not None:
            self._listener.merge_remote_targets(
                enable_filter=msg.get("enable_filter", False),
                group_list=msg.get("group_list", []),
                private_list=msg.get("private_list", []),
            )
            if admin:
                self._listener.set_admin_chats(admin)
            if msg.get("include_muted") is not None:
                self._listener._include_muted = bool(msg["include_muted"])
            logger.info("已同步 Adapter 配置: filter=%s, groups=%s, private=%s, admin=%s",
                         msg.get("enable_filter"), msg.get("group_list"), msg.get("private_list"), admin)
        elif self._weflow is not None:
            logger.info("WeFlow 模式收到配置更新，刷新映射表")
            await asyncio.to_thread(self._weflow.build_mapping)
            logger.info("映射表已刷新: %d names", len(self._weflow._name_to_wxid))

    async def _stop_data_source(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
        if self._weflow is not None:
            self._weflow.stop_push()
            self._weflow = None
        self._sender.stop()
        self._current_mode = None

    async def _start_weflow(self, msg: dict) -> None:
        base_url = msg.get("weflow_base_url", "http://127.0.0.1:5031")
        api_token = msg.get("weflow_api_token", "")
        logger.info("启动 WeFlow 模式: %s", base_url)
        self._weflow = WeFlowClient(base_url=base_url, api_token=api_token)
        if not await asyncio.to_thread(self._weflow.health_check):
            logger.critical("WeFlow 健康检查失败，请确认 weflow serve 已启动")
            self._weflow = None
            return
        logger.info("WeFlow 已就绪，构建映射表...")
        await asyncio.to_thread(self._weflow.build_mapping)
        self._weflow.set_handlers(
            on_message=self._on_wechat_message,
            on_revoke=self._on_wechat_revoke,
        )
        self._sender.set_pre_send_hook(self._on_weflow_pre_send)
        self._sender.set_post_send_hook(self._on_weflow_post_send)
        self._sender.start()
        self._weflow.start_push()
        self._current_mode = "weflow"
        logger.info("WeFlow 模式已启动")

    async def _start_pyweixin(self, msg: dict) -> None:
        admin = msg.get("admin", [])
        group_list = msg.get("group_list", [])
        private_list = msg.get("private_list", [])
        target_chats = group_list + private_list
        logger.info("启动 pyweixin 模式: targets=%s, admin=%s", target_chats, admin)
        listener = WeChatListener(
            target_chats=target_chats,
            excluded=msg.get("excluded", ["文件传输助手", "微信团队", "微信支付"]),
            poll_interval=1.0,
            close_weixin=msg.get("close_weixin", False),
            send_delay=msg.get("send_delay", 0.2),
            on_message=self._on_wechat_message,
            include_muted=msg.get("include_muted", False),
            on_friend_request=self._on_friend_request,
            admin_chats=admin,
        )
        self._listener = listener
        self._sender.set_dialog_windows(listener.get_dialog_windows())
        self._sender.set_pre_send_hook(listener.begin_send)
        self._sender.set_post_send_hook(listener.mark_sent)
        self._sender.start()
        listener.start()
        self._current_mode = "pyweixin"
        logger.info("pyweixin 模式已启动，监听 %d 个会话", len(target_chats) if target_chats else 0)

    def _handle_moment_command(self, msg: dict) -> None:
        """处理来自 Adapter 的朋友圈命令"""
        req_id = msg.get("request_id", "")
        cmd = msg.get("type", "")
        
        if cmd == "moment_read":
            limit = msg.get("limit", 10)
            posts: list[dict] = []
            try:
                if self._weflow is not None:
                    posts = self._read_moments_weflow(limit)
                else:
                    posts = WeChatMoments.read_recent(number=limit)
            except Exception as e:
                logger.error("读取朋友圈失败: %s", e)
            logger.info("朋友圈读取: %d 条", len(posts))
            self._send_moment_response(req_id, {"moments": posts, "count": len(posts)})
        
        elif cmd == "moment_post":
            text = msg.get("text", "")
            try:
                ok = WeChatMoments.post_moment(text=text)
                logger.info("发布朋友圈: %s → %s", text[:40], "成功" if ok else "失败")
            except Exception as e:
                logger.error("发布朋友圈失败: %s", e)
                ok = False
            self._send_moment_response(req_id, {"success": ok, "text": text})

    def _read_moments_weflow(self, limit: int) -> list[dict]:
        """通过 WeFlow API 读取朋友圈，返回完整数据含评论/点赞/图片"""
        raw = self._weflow.get_moments(limit=limit) if self._weflow else []
        posts = []
        for item in raw:
            username = (
                item.get("username")
                or item.get("userName")
                or item.get("wxid")
                or ""
            )
            nickname = (
                item.get("nickname")
                or item.get("nickName")
                or item.get("displayName")
                or item.get("display_name")
                or username
            )
            avatar = item.get("avatarUrl") or item.get("avatar_url") or item.get("avatar") or ""
            content = item.get("content") or item.get("text") or ""
            create_time = item.get("createTime") or item.get("create_time") or 0

            images = []
            media_list = item.get("media") or item.get("images") or item.get("medias") or []
            for m in media_list:
                if isinstance(m, dict):
                    img_url = m.get("url") or m.get("thumb") or m.get("src") or ""
                    if img_url:
                        images.append(img_url)
                elif isinstance(m, str):
                    images.append(m)

            comments = item.get("comments") or []
            if isinstance(comments, list):
                comments = [
                    {
                        "username": c.get("username") or c.get("userName") or c.get("wxid") or "",
                        "nickname": c.get("nickname") or c.get("nickName") or c.get("displayName") or "",
                        "content": c.get("content") or c.get("text") or "",
                        "time": c.get("createTime") or c.get("create_time") or 0,
                    }
                    for c in comments if isinstance(c, dict)
                ]
            else:
                comments = []

            likes = item.get("likes") or item.get("likeList") or []
            if isinstance(likes, list):
                likes = [
                    {
                        "username": l.get("username") or l.get("userName") or l.get("wxid") or (l if isinstance(l, str) else ""),
                        "nickname": l.get("nickname") or l.get("nickName") or l.get("displayName") or "",
                    } if isinstance(l, dict) else {"username": l, "nickname": l}
                    for l in likes
                ]
            else:
                likes = []

            location = item.get("location") or item.get("poiName") or ""
            post_id = item.get("postId") or item.get("post_id") or item.get("timelineId") or item.get("timeline_id") or ""

            posts.append({
                "post_id": str(post_id),
                "author": username,
                "nickname": nickname,
                "avatar": avatar,
                "content": content,
                "time": str(create_time),
                "location": location,
                "images": images,
                "comments": comments,
                "likes": likes,
            })
        return posts

    def _on_weflow_pre_send(self, chat_name: str) -> None:
        """WeFlow 模式发送前钩子 — 无需暂停轮询（轮询自动跳过已读）"""

    def _on_weflow_post_send(self, chat_name: str) -> None:
        """WeFlow 模式发送后钩子 — 更新 watermark，防止回显"""

    def _on_wechat_revoke(self, msg: dict) -> None:
        """处理 WeFlow 撤回事件"""
        rid = msg.get("rawid", "") or msg.get("server_id", "")
        if rid:
            logger.info("消息撤回: rawid=%s", rid)

    def _send_moment_response(self, req_id: str, data: dict) -> None:
        """发送朋友圈响应回 Adapter"""
        payload = {"type": "moment_response", "request_id": req_id, **data}
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._ws.send_inbound(payload),
                self._loop,
            )

    def _on_wechat_message(self, msg: dict) -> None:
        logger.info("微信: [%s] %s: %s", msg["chat"], msg["sender"], msg["content"][:60])
        if self._loop is not None and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self._ws.send_inbound(msg),
                self._loop,
            )
            future.add_done_callback(lambda f: logger.info(
                "发送到 Adapter %s", "成功" if f.result() else "失败"
            ))

    def _on_friend_request(self, msg: dict) -> None:
        logger.info("好友请求: %s", msg.get("content", ""))
        if self._loop is not None and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self._ws.send_inbound(msg),
                self._loop,
            )
            future.add_done_callback(lambda f: logger.info(
                "好友请求发送到 Adapter %s", "成功" if f.result() else "失败"
            ))

    def _on_plugin_outbound(self, msg: dict) -> None:
        msg_type = msg.get("type", "")
        if msg_type == "ack":
            logger.info("Adapter ACK: original=%s success=%s error=%s",
                        msg.get("original_type"), msg.get("success"), msg.get("error", "无"))
            return
        
        # 朋友圈命令处理
        if msg_type in ("moment_read", "moment_post"):
            self._handle_moment_command(msg)
            return

        if msg_type == "friend_approve":
            friend_name = msg.get("friend_name", "")
            if friend_name:
                logger.info("收到好友批准指令: %s", friend_name)
                try:
                    from pyweixin import Contacts
                    Contacts.check_new_friends(verify=True, limit=1, clear=True)
                    logger.info("好友申请已验证: %s", friend_name)
                except Exception as e:
                    logger.warning("验证好友失败: %s", e)
            return

        if msg_type == "friend_dismiss":
            friend_name = msg.get("friend_name", "")
            if friend_name:
                logger.info("收到好友忽略指令: %s", friend_name)
                try:
                    from pyweixin import Contacts
                    Contacts.check_new_friends(verify=False, limit=8, clear=True)
                    logger.info("好友请求已清除: %s", friend_name)
                except Exception as e:
                    logger.warning("清除好友请求失败: %s", e)
            return

        receiver = msg.get("receiver", "")
        segments = msg.get("segments", [])
        at_members = msg.get("at_members", [])
        if not receiver or not segments:
            return
        self._outbound_queue.put({"receiver": receiver, "segments": segments, "at_members": at_members})
        for seg in segments:
            logger.info("MaiBot → [%s]: %s", receiver, seg.get("data", "")[:60])


async def async_main() -> None:
    cfg = cfg_mod.load_config()
    setup_logging(cfg.log)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    client = WeMaiClient(cfg)
    await client.run()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.getLogger("wemai_client.main").critical("异常退出: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
