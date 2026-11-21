from __future__ import annotations

import asyncio
import copy
import time
import uuid
from contextlib import suppress
from datetime import datetime

import aiohttp
from aiocqhttp import Event
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

from .core import (
    ALARM_WINDOW_SECONDS,
    BEIJING,
    DEFAULT_EVENTS,
    DEFAULT_WAKE,
    HHMM,
    KINDS,
    MAX_PENDING,
    StateStore,
    alarm_slot,
    day_at,
    format_notice,
    fresh_state,
    in_quiet_hours,
    wake_prompt,
)
from .history import HistoryError, is_mcp_tool, mounted_tool, read_diary, request_tool, tool_text
from .watch import (
    WATCH_BASE,
    WatchError,
    fish_number,
    idle_blocker,
    on_card_turn_location,
    sleep_signal,
    state_brief,
    validated_state,
    watch_json,
)

API = WATCH_BASE + "/notify"
STATE_API = WATCH_BASE + "/state"
MARKER = "fishworld_wake_id"
IS_TEST = "fishworld_wake_test"
IS_ALARM = "fishworld_wake_alarm"
FALLBACK_ROWS = "fishworld_fallback_rows"
TOOL_VISIBILITY_SECONDS = 120
tool_result_hook = getattr(filter, "on_llm_tool_respond", None)


def settings(config) -> dict:
    def number(name, default, low, high):
        value = int(config.get(name, default))
        return max(low, min(high, value))

    mode = config.get("mode", "agent")
    if mode not in ("agent", "notify"):
        raise ValueError("mode 只能是 agent 或 notify")
    result = {
        "enabled": bool(config.get("enabled", True)),
        "watch_key": str(config.get("watch_key", "")).strip(),
        "mode": mode,
        "interval_seconds": number("interval_seconds", 30, 15, 3600),
        "pause_while_sleeping": bool(config.get("pause_while_sleeping", True)),
        "sleep_check_seconds": number("sleep_check_seconds", 120, 30, 1800),
        "wake_time": str(config.get("wake_time", "")).strip(),
        "mcp_server_name": str(config.get("mcp_server_name", "fish-world")).strip(),
        "max_wakes_per_day": number("max_wakes_per_day", 48, 0, 1000),
        "talk_cooldown_seconds": number("talk_cooldown_seconds", 300, 0, 86400),
        "idle_minutes": number("idle_minutes", 0, 0, 1440),
        "max_idle_wakes_per_day": number("max_idle_wakes_per_day", 12, 0, 1000),
        "busy_timeout_seconds": number("busy_timeout_seconds", 600, 60, 3600),
        "quiet_start": str(config.get("quiet_start", "")).strip(),
        "quiet_end": str(config.get("quiet_end", "")).strip(),
    }
    for name, default in (("event_kinds", DEFAULT_EVENTS), ("wake_kinds", DEFAULT_WAKE)):
        value = config.get(name, default)
        if not isinstance(value, list) or any(x not in KINDS for x in value):
            raise ValueError(f"{name} 应为已知事件类型列表")
        result[name] = set(value)
    in_quiet_hours(result["quiet_start"], result["quiet_end"], time.time())
    if result["wake_time"] and not HHMM.fullmatch(result["wake_time"]):
        raise ValueError("定时起床请用北京时间 HH:MM，或留空关闭")
    return result


@register(
    "astrbot_plugin_fishworld",
    "Fishworld AstrBot Contributors",
    "鱼铃：鱼世界轮询与自动唤醒",
    "0.1.1",
)
class FishWorldPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.store = None
        self.session = None
        self.task = None
        self.lock = asyncio.Lock()
        self.storage_error = False
        self.last_error = ""
        self.last_check = 0.0
        self.last_response = 0.0
        self.last_skipped = 0
        self.failures = 0
        self.next_fetch = 0.0
        self.next_delivery = 0.0
        self.delivery_failures = 0
        self._forget_watch()
        self.tool_visibility = None
        self.last_tool_result = ""
        self.idle_status = ""
        self.alarm_status = ""
        self.diary_busy = False

    def _forget_watch(self):
        self.snapshot = None
        self.snapshot_at = 0.0
        self.next_state = 0.0
        self.state_failures = 0
        self.state_error = ""

    def _fresh_snapshot(self, cfg, now):
        max_age = min(cfg["interval_seconds"], 60) + 10
        return (
            self.snapshot
            if self.snapshot is not None and 0 <= now - self.snapshot_at <= max_age
            else None
        )

    def _use_watch_key(self, key):
        try:
            changed = self.store.use_key(key, time.time())
        except (OSError, ValueError, TypeError):
            self.storage_error = True
            raise
        if changed:
            self._forget_watch()
            self.tool_visibility = None
            self.last_tool_result = self.idle_status = self.last_error = ""
            self.alarm_status = ""
            self.next_fetch = self.next_delivery = 0.0
            self.last_check = self.last_response = self.last_skipped = self.failures = 0

    async def initialize(self):
        self.path = StarTools.get_data_dir("astrbot_plugin_fishworld") / "state.json"
        try:
            self.store = StateStore(self.path, time.time())
        except (OSError, ValueError, TypeError):
            self.storage_error = True
            self.last_error = "状态文件无法读取，已停止轮询；备份后可用 /鱼世界 重置确认 重建"
            logger.error("[鱼世界] " + self.last_error)
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self.task = asyncio.create_task(self._loop(), name="fishworld-poller")

    async def terminate(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        if self.session:
            await self.session.close()

    def _save(self):
        try:
            self.store.save()
        except (OSError, ValueError, TypeError):
            self.storage_error = True
            raise RuntimeError("状态写入失败，已停止投递，请检查磁盘权限并重载插件") from None

    async def _loop(self):
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Do not log exception bodies: remote bodies and configs may contain secrets.
                if not self.storage_error:
                    self.last_error = "轮询配置或本地处理异常，请检查配置和状态目录后重载"
                else:
                    self.last_error = "状态不可用，轮询已停止；检查文件和写入权限后重载插件"
                logger.error("[鱼世界] " + self.last_error)
            await asyncio.sleep(2)

    async def check(self):
        async with self.lock:
            if self.storage_error or self.store is None:
                return
            cfg = settings(self.config)
            state = self.store.data
            now = time.time()
            busy = state["busy"]
            if busy and now - busy["started"] >= cfg["busy_timeout_seconds"]:
                state["busy"] = None
                state["last_delivery"] = "上次唤醒等待超时，执行结果未知；不会自动重试该批事件"
                if busy.get("test") and not self.last_tool_result:
                    self.last_tool_result = "未观察到可验证的工具结果，测试等待超时"
                self._save()
            if not cfg["enabled"] or state["paused"] or not state["binding"]:
                return
            key = cfg["watch_key"]
            if not key:
                return
            if not key.startswith("fwr_") or any(c.isspace() for c in key):
                self.last_error = "请填写 fwr_ 开头的观看密钥，不能填写 MCP 操作密钥"
                return
            self._use_watch_key(key)
            state = self.store.data
            now = time.time()
            self.store.roll_day(now)
            need_state = (
                cfg["pause_while_sleeping"]
                or bool(cfg["wake_time"] and cfg["mode"] == "agent")
                or (cfg["mode"] == "agent" and cfg["idle_minutes"] > 0)
                or ("cards" in cfg["event_kinds"] and "cards" in cfg["wake_kinds"])
            )
            slot = alarm_slot(cfg["wake_time"], now) if cfg["mode"] == "agent" else None
            needs_alarm_state = bool(
                slot
                and state["alarm_day"] < slot[0]
                and not self.state_error
                and self.snapshot_at < slot[1]
                and state["sleeping"]
            )
            if (
                need_state
                and self.session is not None
                and (now >= self.next_state or needs_alarm_state)
            ):
                await self._read_state(cfg, now)
            if await self._check_alarm(cfg, time.time()):
                return
            if cfg["pause_while_sleeping"] and state["sleeping"]:
                return
            now = time.time()
            if now >= self.next_fetch and len(state["pending"]) <= MAX_PENDING - 50:
                await self._fetch(cfg, now)
            # Disabled kinds already received are discarded rather than waking later.
            disabled = [r for r in state["pending"] if r["item"]["kind"] not in cfg["event_kinds"]]
            if disabled:
                self.store.consume(disabled)
                self._save()
            if time.time() >= self.next_delivery:
                await self._deliver(cfg, time.time())

    async def _check_alarm(self, cfg, now):
        state = self.store.data
        slot = alarm_slot(cfg["wake_time"], now)
        if not slot or cfg["mode"] != "agent":
            return False
        day, due = slot
        if state["alarm_day"] >= day:
            return False
        snapshot = self.snapshot
        if (
            self.state_error
            or snapshot is None
            or not due <= self.snapshot_at <= now
            or now - self.snapshot_at > cfg["sleep_check_seconds"] + 10
        ):
            self.alarm_status = "等待到点后的有效状态"
            return False
        signal = sleep_signal(snapshot)
        if signal is False:
            state["alarm_day"] = day
            self.alarm_status = "确认已醒，今天无需闹钟"
            self._save()
            return False
        fish = fish_number(snapshot)
        if signal is not True or not fish:
            self.alarm_status = "无法确认睡眠或鱼的编号"
            return False
        if in_quiet_hours(cfg["quiet_start"], cfg["quiet_end"], now):
            self.alarm_status = "免打扰中"
        elif state["busy"]:
            self.alarm_status = "等待已提交的会话结束"
        elif state["wake_count"] >= cfg["max_wakes_per_day"]:
            self.alarm_status = "每日唤醒额度不足"
        elif now < self.next_delivery:
            self.alarm_status = "等待投递退避结束"
        elif not self._alarm_tools_ready(cfg):
            self.alarm_status = "未找到配置的鱼世界 world_now 和 world_do"
        else:
            try:
                self._dispatch(cfg, now, [], alarm=(day, due, fish))
            except Exception:
                if self.storage_error:
                    raise
                self.next_delivery = now + 60
                self.alarm_status = (
                    "提交结果未知，当天不会自动再试"
                    if state["alarm_day"] == day
                    else "暂不能提交，请检查绑定的 QQ 会话"
                )
            else:
                self.alarm_status = "已提交起床任务，等待鱼世界状态确认"
                return True
        return False

    def _alarm_tools_ready(self, cfg):
        try:
            server = cfg["mcp_server_name"]
            return bool(
                server
                and all(
                    mounted_tool(self.context, name, server) for name in ("world_now", "world_do")
                )
            )
        except (AttributeError, TypeError, RuntimeError):
            return False

    async def _fetch(self, cfg, now):
        self.last_check = now
        self.next_fetch = now + cfg["interval_seconds"]
        try:
            params = {"cursor": self.store.data["cursor"]} if self.store.data["cursor"] else {}
            payload = await watch_json(self.session, API, cfg["watch_key"], now, params)
            self.store.ingest(payload, cfg["event_kinds"], time.time())
            skipped = payload.get("skipped", 0)
            self.last_skipped = skipped if type(skipped) is int and skipped > 0 else 0
            self.last_response = time.time()
            self.failures = 0
            self.last_error = ""
        except WatchError as exc:
            self._fetch_failed(f"通知接口 HTTP {exc.status}", cfg, now, exc.retry)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            self._fetch_failed("通知接口网络错误或超时", cfg, now)
        except (ValueError, TypeError, UnicodeError):
            self._fetch_failed("通知接口响应格式无效，保留原游标", cfg, now)
        except OSError:
            self.storage_error = True
            raise

    def _fetch_failed(self, message, cfg, now, retry=0):
        self.failures += 1
        delay = max(retry, min(900, cfg["interval_seconds"] * 2 ** min(self.failures, 5)))
        self.next_fetch = now + delay
        self.last_error = f"{message}；约 {delay} 秒后重试"
        logger.warning("[鱼世界] " + self.last_error)

    async def _read_state(self, cfg, now):
        state = self.store.data
        sleeping = cfg["pause_while_sleeping"] and state["sleeping"]
        interval = cfg["sleep_check_seconds"] if sleeping else min(cfg["interval_seconds"], 60)
        self.next_state = now + interval
        try:
            snapshot = validated_state(
                await watch_json(self.session, STATE_API, cfg["watch_key"], now)
            )
        except WatchError as exc:
            self._state_failed(now, interval, f"状态接口 HTTP {exc.status}", exc.retry)
            return
        except (ValueError, TypeError, aiohttp.ClientError, asyncio.TimeoutError):
            self._state_failed(now, interval, "状态接口失败、超时或返回格式无效")
            return
        self.snapshot, self.snapshot_at = snapshot, time.time()
        self.state_failures = 0
        self.state_error = ""
        signal = sleep_signal(snapshot)
        if signal is not None and state["sleeping"] != signal:
            state["sleeping"] = signal
            self._save()
        if signal is True and cfg["pause_while_sleeping"]:
            self.next_state = self.snapshot_at + cfg["sleep_check_seconds"]
        elif sleeping and signal is False:
            self.next_state = self.snapshot_at + min(cfg["interval_seconds"], 60)
            if not self.failures:
                self.next_fetch = min(self.next_fetch, self.snapshot_at)
        if signal is False and self.alarm_status in (
            "已提交起床任务，等待鱼世界状态确认",
            "会话已收到起床任务，尚未确认实际起床",
        ):
            self.alarm_status = "观看接口已确认鱼醒了"

    def _state_failed(self, now, interval, reason, retry=0):
        self.state_failures += 1
        self.next_state = now + max(retry, min(1800, interval * 2 ** min(self.state_failures, 4)))
        self.state_error = reason

    def _adapter(self):
        binding = self.store.data["binding"]
        for adapter in self.context.platform_manager.get_insts():
            if adapter.meta().id == binding["platform_id"] and adapter.meta().name == "aiocqhttp":
                return adapter
        raise RuntimeError("绑定的 NapCat / OneBot v11 平台未加载")

    def _make_event(self, prompt):
        adapter = self._adapter()
        binding = self.store.data["binding"]
        message = AstrBotMessage()
        message.type = MessageType(binding["message_type"])
        message.self_id = binding["self_id"]
        message.session_id = binding["session_id"]
        message.message_id = "fishworld_" + uuid.uuid4().hex
        message.sender = MessageMember(user_id=binding["sender_id"], nickname="鱼铃后台提醒")
        message.group_id = binding["group_id"]
        message.message = [Plain(prompt)]
        message.message_str = prompt
        raw = {
            "time": int(time.time()),
            "self_id": int(binding["self_id"]),
            "post_type": "message",
            "message_type": "group" if binding["group_id"] else "private",
            "sub_type": "normal" if binding["group_id"] else "friend",
            "message_id": 0,
            "user_id": int(binding["sender_id"]),
            "message": [{"type": "text", "data": {"text": prompt}}],
            "raw_message": prompt,
            "sender": {"user_id": int(binding["sender_id"]), "nickname": "鱼铃后台提醒"},
        }
        if binding["group_id"]:
            raw["group_id"] = int(binding["group_id"])
        message.raw_message = Event(raw)
        # Constructor unchanged between AstrBot v4.5.7 and the checked current source.
        event = AiocqhttpMessageEvent(
            message_str=prompt,
            message_obj=message,
            platform_meta=adapter.meta(),
            session_id=binding["session_id"],
            bot=adapter.get_client(),
        )
        event.is_wake = True
        event.is_at_or_wake_command = True
        event.set_extra(MARKER, uuid.uuid4().hex)
        return adapter, event

    def _dispatch(self, cfg, now, rows, *, idle=False, test=False, alarm=None):
        snapshot = self._fresh_snapshot(cfg, now)
        prompt_state = state_brief(snapshot) if snapshot and not test else ""
        adapter, event = self._make_event(
            wake_prompt(
                rows,
                idle=idle,
                test=test,
                snapshot=prompt_state,
                alarm_fish=alarm[2] if alarm else "",
            )
        )
        if test:
            event.set_extra(IS_TEST, True)
        elif rows:
            event.set_extra(FALLBACK_ROWS, rows)
        previous = copy.deepcopy(self.store.data)
        state = self.store.data
        if alarm:
            event.set_extra(IS_ALARM, (alarm[1], cfg["wake_time"], cfg["mcp_server_name"]))
            state["alarm_day"] = alarm[0]
        self.store.consume(rows)
        state["wake_count"] += 1
        state["idle_count"] += int(idle)
        state["last_wake"] = now
        for row in rows:
            if row["item"]["kind"] == "talk":
                state["talk_at"][str(row["item"].get("from", ""))] = now
        state["busy"] = {"id": event.get_extra(MARKER), "started": now, "test": test}
        state["last_delivery"] = "已提交 AstrBot 会话队列，等待模型运行"
        # Reserve before queuing: a crash must not automatically repeat world actions.
        self._save()
        try:
            adapter.commit_event(event)
        except Exception:
            if alarm:
                state["busy"] = None
                state["last_delivery"] = "闹钟提交结果未知，当天不会自动再试"
            else:
                self.store.data = previous
            self._save()
            raise
        if test:
            self.last_tool_result = ""

    def _mcp_ready(self, now):
        # An unavailable global tool is enough to fall back; its presence doesn't prove
        # that the target session exposes it to the model.
        getter = getattr(self.context, "get_llm_tool_manager", None)
        if getter is not None:
            try:
                manager = getter()
                tools = getattr(manager, "func_list", None)
                if tools is None:
                    tools = (manager.get_full_tool_set().get_tool("world_now"),)
                if not any(
                    getattr(tool, "name", None) == "world_now" and getattr(tool, "active", True)
                    for tool in tools
                ):
                    return False
            except (AttributeError, TypeError, RuntimeError):
                pass
        if self.tool_visibility is not None:
            origin, checked, exposed = self.tool_visibility
            binding = self.store.data["binding"]
            if (
                binding
                and origin == binding["umo"]
                and 0 <= now - checked < TOOL_VISIBILITY_SECONDS
            ):
                return exposed
        return True

    async def _deliver(self, cfg, now):
        state = self.store.data
        if state["busy"]:
            return
        snapshot = self._fresh_snapshot(cfg, now)
        effective = cfg
        if cfg["mode"] == "agent" and not self._mcp_ready(now):
            effective = {**cfg, "mode": "notify"}
        agent, notify = self.store.choose(
            effective, now, cards_ready=bool(snapshot and on_card_turn_location(snapshot))
        )
        try:
            if notify:
                sent = await self.context.send_message(
                    state["binding"]["umo"], MessageChain().message(format_notice(notify))
                )
                if not sent:
                    raise RuntimeError("绑定的平台不存在")
                self.store.consume(notify)
                state["last_delivery"] = "普通 QQ 提醒已发送"
                self._save()
            if agent:
                self._dispatch(cfg, now, agent)
            elif not state["pending"] and self._idle_due(effective, now):
                self.idle_status = (
                    "通知查询异常或没有近期有效响应"
                    if self.failures
                    or not 0 <= now - self.last_response <= cfg["interval_seconds"] * 2
                    else idle_blocker(snapshot, now)
                    if snapshot and not self.state_error
                    else "没有新鲜状态"
                )
                if not self.idle_status:
                    self._dispatch(cfg, now, [], idle=True)
            self.delivery_failures = 0
        except Exception:
            if self.storage_error:
                raise
            self.delivery_failures += 1
            delay = min(900, 30 * 2 ** min(self.delivery_failures, 5))
            self.next_delivery = now + delay
            self.last_error = f"QQ 投递或唤醒提交失败，约 {delay} 秒后重试；检查 NapCat 连接"
            logger.warning("[鱼世界] " + self.last_error)

    def _idle_due(self, cfg, now):
        state = self.store.data
        return (
            cfg["mode"] == "agent"
            and cfg["idle_minutes"] > 0
            and bool(state["cursor"])
            and state["wake_count"] < cfg["max_wakes_per_day"]
            and state["idle_count"] < cfg["max_idle_wakes_per_day"]
            and now - state["last_wake"] >= max(5, cfg["idle_minutes"]) * 60
            and not in_quiet_hours(cfg["quiet_start"], cfg["quiet_end"], now)
        )

    @filter.on_llm_request()
    async def on_request(self, event: AstrMessageEvent, request):
        if event.get_extra(MARKER):
            async with self.lock:
                if (
                    self.store
                    and self.store.data["busy"]
                    and self.store.data["busy"]["id"] == event.get_extra(MARKER)
                ):
                    tool = request_tool(request, "world_now")
                    self.tool_visibility = (event.unified_msg_origin, time.time(), tool is not None)
                    self.store.data["last_delivery"] = "模型已启动；" + (
                        "当前会话可见 world_now" if tool is not None else "当前会话未挂载 world_now"
                    )
                    if request is not None and hasattr(request, "func_tool"):
                        alarm = event.get_extra(IS_ALARM)
                        if alarm:
                            due, saved_time, server = alarm
                            cfg = settings(self.config)
                            now = time.time()
                            action = request_tool(request, "world_do")
                            allowed = (
                                cfg["enabled"]
                                and not self.store.data["paused"]
                                and cfg["mode"] == "agent"
                                and cfg["wake_time"] == saved_time
                                and cfg["mcp_server_name"] == server
                                and due <= now < due + ALARM_WINDOW_SECONDS
                                and not in_quiet_hours(cfg["quiet_start"], cfg["quiet_end"], now)
                                and is_mcp_tool(tool, "world_now", server)
                                and is_mcp_tool(action, "world_do", server)
                            )
                            request.func_tool = (
                                type(request.func_tool)([tool, action]) if allowed else None
                            )
                            self.alarm_status = (
                                "会话已收到起床任务，尚未确认实际起床"
                                if allowed
                                else "起床任务已失效或目标会话缺少鱼世界工具"
                            )
                        elif event.get_extra(IS_TEST):
                            # Limit only this synthetic diagnostic request. Don't alter registered tools.
                            request.func_tool = type(request.func_tool)([tool]) if tool else None
                        elif tool is None:
                            request.func_tool = None
                            fallback = event.get_extra(FALLBACK_ROWS) or []
                            event.set_extra(FALLBACK_ROWS, None)
                            pending = self.store.data["pending"]
                            known = {row["id"] for row in pending}
                            rows = [row for row in fallback if row["id"] not in known]
                            room = MAX_PENDING - len(pending)
                            pending[:0] = rows[:room]
                            if rows[:room]:
                                self.store.data["last_delivery"] += "；事件已转普通提醒队列"
                    self._save()

    @(tool_result_hook or (lambda: lambda callback: callback))()
    async def on_tool_result(self, event: AstrMessageEvent, tool, _tool_args, tool_result):
        if not event.get_extra(MARKER) or getattr(tool, "name", None) != "world_now":
            return
        async with self.lock:
            busy = self.store.data["busy"] if self.store else None
            if not busy or busy["id"] != event.get_extra(MARKER):
                return
            success = bool(tool_text(tool_result))
            self.last_tool_result = (
                "world_now 实际调用成功" if success else "world_now 调用失败或没有返回可读数据"
            )
            if not success:
                self.tool_visibility = (event.unified_msg_origin, time.time(), False)

    @filter.on_llm_response()
    async def on_response(self, event: AstrMessageEvent, response):
        if event.get_extra(MARKER) and not getattr(response, "tools_call_name", []):
            async with self.lock:
                if (
                    self.store
                    and self.store.data["busy"]
                    and self.store.data["busy"]["id"] == event.get_extra(MARKER)
                ):
                    test = self.store.data["busy"].get("test")
                    self.store.data["busy"] = None
                    status = "模型已返回最终响应"
                    if test:
                        if self.last_tool_result:
                            status += "；" + self.last_tool_result
                        elif self.tool_visibility and not self.tool_visibility[2]:
                            status += "；当前会话未挂载 world_now"
                        else:
                            status += (
                                "；没有验证到工具结果"
                                if tool_result_hook
                                else ("；此 AstrBot 版本没有工具结果回调，请核对工具日志")
                            )
                    self.store.data["last_delivery"] = status
                    self._save()

    def _status(self):
        def stamp(value):
            return (
                datetime.fromtimestamp(value, BEIJING).strftime("%m-%d %H:%M:%S")
                if value
                else "暂无"
            )

        cfg = settings(self.config)
        state = self.store.data
        self.store.roll_day(time.time())
        sleeping = cfg["pause_while_sleeping"] and state["sleeping"]
        sleep_status = (
            f"睡眠暂停中；预计 {stamp(self.next_state)} 再检查是否醒来"
            if sleeping
            else "未进入睡眠暂停"
            if cfg["pause_while_sleeping"]
            else "睡眠暂停已关闭"
        )
        return (
            f"鱼世界状态（北京时间）\n启用：{cfg['enabled']}；暂停：{state['paused']}；模式：{cfg['mode']}\n"
            f"观看密钥已填写：{bool(cfg['watch_key'])}（不代表鉴权成功）\n"
            f"绑定：{state['binding']['umo'] if state['binding'] else '尚未绑定'}\n"
            f"间隔：{cfg['interval_seconds']} 秒；基准已建立：{bool(state['cursor'])}\n"
            f"最近请求：{stamp(self.last_check)}；最近有效响应：{stamp(self.last_response)}\n"
            f"睡眠：{sleep_status}；最近状态：{stamp(self.snapshot_at)}\n"
            f"定时起床：{cfg['wake_time'] or '关闭'}"
            + (
                f"；上次处理日期：{state['alarm_day']}"
                if cfg["wake_time"] and state["alarm_day"]
                else ""
            )
            + (f"；{self.alarm_status}" if cfg["wake_time"] and self.alarm_status else "")
            + "\n"
            f"状态接口：{self.state_error or ('正常' if self.snapshot_at else '尚未检查')}\n"
            f"待处理：{len(state['pending'])}；服务端 skipped：{self.last_skipped}\n"
            f"今日唤醒：{state['wake_count']}/{cfg['max_wakes_per_day']}；其中闲时：{state['idle_count']}/{cfg['max_idle_wakes_per_day']}\n"
            f"闲时检查：{cfg['idle_minutes']} 分钟（0 为关闭，启用后最少 5 分钟）\n"
            + (f"上次跳过闲时唤醒：{self.idle_status}\n" if self.idle_status else "")
            + f"免打扰：{in_quiet_hours(cfg['quiet_start'], cfg['quiet_end'], time.time())}；等待模型：{bool(state['busy'])}\n"
            f"投递：{state['last_delivery']}\n"
            f"MCP 工具结果：{self.last_tool_result or '尚未验证'}\n最近错误：{self.last_error or '无'}"
        )

    def _private_owner(self, event):
        binding = self.store and self.store.data["binding"]
        return bool(
            binding
            and event.get_platform_name() == "aiocqhttp"
            and not event.get_group_id()
            and event.get_platform_id() == binding["platform_id"]
            and event.get_self_id() == binding["self_id"]
            and event.get_sender_id() == binding["sender_id"]
        )

    async def _current_status(self, event):
        binding = self.store.data["binding"]
        if not binding or (
            binding["umo"] != event.unified_msg_origin and not self._private_owner(event)
        ):
            return "请在绑定会话，或由绑定主人私聊查询现状。"
        cfg = settings(self.config)
        key = cfg["watch_key"]
        if not key.startswith("fwr_") or any(c.isspace() for c in key):
            return "请在 AstrBot 插件配置里填写 fwr_ 观看密钥。"
        try:
            self._use_watch_key(key)
        except (OSError, ValueError, TypeError):
            return "状态无法保存，已停止自动投递；请检查状态目录。"
        now = time.time()
        fresh = self._fresh_snapshot(cfg, now)
        if fresh is None and (not self.state_failures or now >= self.next_state):
            if self.session is not None:
                await self._read_state(cfg, now)
                fresh = self._fresh_snapshot(cfg, time.time())
        if fresh is not None:
            return state_brief(fresh)
        return f"暂时无法读取当前状态：{self.state_error or '请稍后重试'}。"

    async def _owner_diary(self, event):
        async with self.lock:
            if self.storage_error or not self._private_owner(event):
                return "请由绑定的 QQ 管理员私聊机器人查询日记，群聊不会返回原文。"
            if self.diary_busy:
                return "正在读取日记，请稍后。"
            binding = dict(self.store.data["binding"])
            server = settings(self.config)["mcp_server_name"]
            if not server:
                return "请先在 AstrBot 插件配置里填写已接入的鱼世界 MCP 名称。"
            self.diary_busy = True
        try:
            reply = await read_diary(self.context, event, server, day_at(time.time()))
        except HistoryError as exc:
            reply = str(exc)
        except Exception:
            reply = "读取日记失败或超时，请检查鱼世界 MCP 是否已连接。"
        finally:
            async with self.lock:
                self.diary_busy = False
        if (
            self.storage_error
            or not self.store
            or binding != self.store.data["binding"]
            or server != settings(self.config)["mcp_server_name"]
        ):
            return "绑定已变更，本次日记不会发送。"
        return reply

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("鱼世界")
    async def command(self, event: AstrMessageEvent, action: str = "帮助"):
        if event.get_extra(MARKER):
            return
        if action == "今日日记":
            yield event.plain_result(await self._owner_diary(event))
            return
        if action == "检查":
            await self.check()  # Does not bypass interval, Retry-After, pause or quotas.
        async with self.lock:
            if action == "重置确认":
                if self.path.exists():
                    backup = self.path.with_name(f"state.backup-{time.time_ns()}.json")
                    backup.write_bytes(self.path.read_bytes())
                old = self.store.data if self.store else {}
                self.store = StateStore.__new__(StateStore)
                self.store.path = self.path
                self.store.data = fresh_state(time.time())
                for key in (
                    "binding",
                    "paused",
                    "sleeping",
                    "alarm_day",
                    "busy",
                    "day",
                    "wake_count",
                    "idle_count",
                    "talk_at",
                    "last_wake",
                ):
                    if key in old:
                        self.store.data[key] = old[key]
                self.store.data["key_hash"] = old.get("key_hash", "")
                self._save()
                self.storage_error = False
                self.last_error = ""
                self.next_fetch = 0
                self._forget_watch()
                self.tool_visibility = None
                self.last_tool_result = ""
                reply = (
                    "已备份并重建状态，清除待处理事件；下一次只建立新基准，不补历史。今日额度保持。"
                )
            elif self.storage_error or not self.store:
                reply = "状态文件不可用。检查状态目录权限后重载；若需丢弃旧队列并重新建立基准，发送 /鱼世界 重置确认。"
            elif action == "绑定":
                if event.get_platform_name() != "aiocqhttp":
                    reply = "请在已连接 NapCat 的 OneBot v11 QQ 会话里绑定。"
                else:
                    old = self.store.data["binding"]
                    if old and old["umo"] != event.unified_msg_origin:
                        reply = "已有其他会话绑定。请先发送 /鱼世界 解绑，再在目标会话绑定。"
                    else:
                        self.store.data["binding"] = {
                            "platform_id": event.get_platform_id(),
                            "umo": event.unified_msg_origin,
                            "session_id": event.message_obj.session_id,
                            "message_type": event.message_obj.type.value,
                            "sender_id": event.get_sender_id(),
                            "self_id": event.get_self_id(),
                            "group_id": event.get_group_id() or "",
                        }
                        self._save()
                        reply = "已绑定当前会话。配置 fwr_ 观看密钥后自动轮询；首次仅建立基准。"
            elif action == "解绑":
                if self.store.data["busy"]:
                    reply = (
                        "当前仍有已提交的唤醒，请等本轮结束或等待超时后解绑。暂停只能阻止后续唤醒。"
                    )
                else:
                    self.store.data["binding"] = None
                    self._save()
                    reply = "已解绑，停止轮询。已有待处理事件保留，重新绑定后可能投递到新会话。"
            elif action in ("暂停", "继续"):
                self.store.data["paused"] = action == "暂停"
                self._save()
                if action == "暂停":
                    reply = "已暂停后续轮询和唤醒，已运行的模型不受影响。"
                elif settings(self.config)["pause_while_sleeping"] and self.store.data["sleeping"]:
                    reply = "手动暂停已解除；鱼仍在睡眠暂停中，确认醒来后会继续。"
                else:
                    reply = "已解除手动暂停。"
            elif action in ("状态", "检查"):
                reply = self._status()
            elif action == "现状":
                reply = await self._current_status(event)
            elif action == "试唤醒":
                cfg = settings(self.config)
                self.store.roll_day(time.time())
                if (
                    not self.store.data["binding"]
                    or self.store.data["binding"]["umo"] != event.unified_msg_origin
                ):
                    reply = "请在绑定会话执行试唤醒。"
                elif self.store.data["busy"]:
                    reply = "上一次唤醒尚未结束，请稍后再试。"
                elif self.store.data["wake_count"] >= cfg["max_wakes_per_day"]:
                    reply = "今日唤醒额度已用完。"
                else:
                    self._dispatch(cfg, time.time(), [], test=True)
                    reply = "已提交一次只读测试：让机器人调用 world_now 查看现状，会消耗模型额度。"
            else:
                reply = (
                    "管理员命令：\n/鱼世界 绑定\n/鱼世界 状态\n/鱼世界 检查\n"
                    "/鱼世界 现状\n/鱼世界 今日日记（仅绑定主人私聊）\n"
                    "/鱼世界 试唤醒（只读 MCP 测试，消耗模型额度）\n/鱼世界 暂停\n"
                    "/鱼世界 继续\n/鱼世界 解绑\n/鱼世界 重置确认（备份并丢弃旧队列，重建基准）\n"
                    "密钥只在 AstrBot 插件配置页填写，不要发进 QQ。"
                )
        yield event.plain_result(reply)
