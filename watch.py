from __future__ import annotations

import json
import math
import re
from datetime import datetime
from email.utils import parsedate_to_datetime

WATCH_BASE = "https://api.yuyuworld.app/v1"
PRIVATE_TOKEN = re.compile(r"\b(?:fwr?_[A-Za-z0-9_./+=-]+)\b")


class WatchError(ValueError):
    def __init__(self, status: int, retry: int):
        self.status = status
        self.retry = retry


def retry_after(value: str, now: float) -> int:
    try:
        try:
            delay = int(value)
        except ValueError:
            when = parsedate_to_datetime(value)
            delay = math.ceil(when.timestamp() - now) if when.tzinfo is not None else 0
        return min(86400, max(0, delay))
    except (ValueError, TypeError, OverflowError):
        return 0


async def watch_json(session, url: str, key: str, now: float, params=None):
    async with session.get(
        url,
        params=params,
        headers={"Authorization": "Bearer " + key},
        allow_redirects=False,
    ) as response:
        if response.status != 200:
            raise WatchError(
                response.status,
                retry_after(response.headers.get("Retry-After", ""), now)
                if response.status == 429
                else 0,
            )
        body = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            body.extend(chunk)
            if len(body) > 512000:
                raise ValueError("观看响应过大")
        return json.loads(body)


def field(state: dict, section: str) -> dict:
    value = state.get(section)
    return value if isinstance(value, dict) else {}


def sleep_signal(state: dict) -> bool | None:
    """A positive /v1/state signal, not QQ presence or a guess from the activity text."""
    figure = field(state, "figure")
    if type(figure.get("asleep")) is bool:
        return figure["asleep"]
    if state.get("stage") == "onboarding":
        return False
    pose = figure.get("pose")
    if pose == "sleep":
        return True
    # doze may mean an offline passenger; without asleep it isn't a reliable signal.
    if isinstance(pose, str) and pose and pose != "doze":
        return False
    return None


def validated_state(value) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("fish"), str) or not value["fish"]:
        raise ValueError("鱼世界状态格式无效")
    return value


def idle_blocker(state: dict, now: float) -> str:
    if sleep_signal(state) is not False:
        return "正在睡觉或还不能确认已醒"
    if state.get("stage") not in (None, "live"):
        return "尚未入住"
    if field(state, "where").get("kind") == "trip":
        return "正在赶路"
    doing = field(state, "doing")
    until = doing.get("until")
    if isinstance(until, str):
        try:
            when = datetime.fromisoformat(until.replace("Z", "+00:00"))
            if when.tzinfo is not None and when.timestamp() > now:
                return "活动尚未结束"
        except (ValueError, OverflowError):
            pass
    elif isinstance(until, (int, float)) and not isinstance(until, bool):
        if (until / 1000 if until > 10**12 else until) > now:
            return "活动尚未结束"
    return ""


def on_card_turn_location(state: dict) -> bool:
    where = field(state, "where")
    place = where.get("place")
    place_id = place.get("id") if isinstance(place, dict) else place
    return where.get("kind") == "place" and place_id == "fulai-cards"


def plain_text(value: str, limit: int) -> str:
    safe = PRIVATE_TOKEN.sub("[密钥已隐藏]", value).replace("[CQ:", "［CQ:")
    return safe[:limit]


def state_brief(state: dict) -> str:
    def short(value, default="未知"):
        return (
            plain_text(value.strip(), 160) if isinstance(value, str) and value.strip() else default
        )

    where, doing, body = (field(state, k) for k in ("where", "doing", "body"))
    asleep = sleep_signal(state)
    return "\n".join(
        (
            f"【鱼世界现状】{short(state['fish'])}",
            f"位置：{short(where.get('text'))}",
            f"活动：{short(doing.get('name'), '没有正在进行的活动')}",
            f"睡眠：{ {True: '正在睡觉', False: '清醒', None: '无法确认'}[asleep] }",
            f"身体：{short(body.get('text'))}"
            + (f"；生病：{short(body['ill'], '是')}" if body.get("ill") else ""),
        )
    )
