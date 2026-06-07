from __future__ import annotations

import logging


logger = logging.getLogger("wemai_client.moments")


class WeChatMoments:
    def __init__(self) -> None:
        pass

    @staticmethod
    def read_recent(number: int = 10) -> list[dict]:
        from pyweixin import GlobalConfig
        from pyweixin.WeChatAuto import Moments
        import time as _time
        GlobalConfig.close_weixin = False
        for attempt in range(3):
            try:
                _time.sleep(1.0)
                posts = Moments.dump_recent_posts(
                    recent="Today", number=number, with_name=False,
                    save_detail=False, close_weixin=False,
                )
                if isinstance(posts, list):
                    simplified = []
                    for p in posts[:number]:
                        simplified.append({
                            "author": str(p.get("好友", "")),
                            "content": str(p.get("内容", "")),
                            "time": str(p.get("发布时间", "")),
                            "images": int(p.get("图片数量", 0)),
                        })
                    return simplified
            except Exception as e:
                logger.warning("读取朋友圈失败(第%d次): %s", attempt + 1, e)
                _time.sleep(1.5)
        return []

    @staticmethod
    def post_moment(text: str, medias: list[str] | None = None) -> bool:
        from pyweixin.WeChatAuto import Moments
        try:
            Moments.post_moments(
                text=text, medias=medias or [],
                close_weixin=False,
            )
            return True
        except Exception as e:
            logger.error("发布朋友圈失败: %s", e)
            return False

    @staticmethod
    def read_friend_moments(friend: str, number: int = 5) -> list[dict]:
        from pyweixin.WeChatAuto import Moments
        try:
            posts = Moments.dump_friend_posts(
                friend=friend, number=number, with_name=False,
                save_detail=False, close_weixin=False,
            )
            if isinstance(posts, list):
                simplified = []
                for p in posts[:number]:
                    simplified.append({
                        "author": str(p.get("好友", "")),
                        "content": str(p.get("内容", "")),
                        "time": str(p.get("发布时间", "")),
                        "images": int(p.get("图片数量", 0)),
                    })
                return simplified
        except Exception as e:
            logger.warning("读取好友朋友圈失败: %s", e)
        return []
