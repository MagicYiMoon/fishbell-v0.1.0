"""Offline tests: real HTTP client, simulated AstrBot/OneBot interfaces.

The stubs intentionally do not simulate a model successfully using MCP.
"""

import asyncio
import importlib
import importlib.util
import logging
import sys
import tempfile
import types
import unittest
from datetime import timedelta
from email.utils import format_datetime
from enum import Enum
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web

package_dir = Path(__file__).resolve().parents[1]
package_spec = importlib.util.spec_from_file_location(
    "astrbot_plugin_fishworld",
    package_dir / "__init__.py",
    submodule_search_locations=[str(package_dir)],
)
package = importlib.util.module_from_spec(package_spec)
sys.modules["astrbot_plugin_fishworld"] = package
package_spec.loader.exec_module(package)


def install_stubs():
    def module(name, **members):
        obj = types.ModuleType(name)
        obj.__dict__.update(members)
        sys.modules[name] = obj
        return obj

    def decorator(*args, **kwargs):
        return lambda func: func

    class MessageType(Enum):
        FRIEND_MESSAGE = "FriendMessage"
        GROUP_MESSAGE = "GroupMessage"

    class Message:
        pass

    class Member:
        def __init__(self, user_id, nickname):
            self.user_id, self.nickname = user_id, nickname

    class Chain:
        def message(self, text):
            self.text = text
            return self

    class Plain:
        def __init__(self, text):
            self.text = text

    class Star:
        def __init__(self, context):
            self.context = context

    class FakeEvent:
        def __init__(self, message_str, message_obj, platform_meta, session_id, bot):
            self.message_str, self.message_obj = message_str, message_obj
            self.platform_meta, self.session_id, self.bot = platform_meta, session_id, bot
            self.unified_msg_origin = f"{platform_meta.id}:{message_obj.type.value}:{session_id}"
            self.extras = {}

        def set_extra(self, key, value):
            self.extras[key] = value

        def get_extra(self, key, default=None):
            return self.extras.get(key, default)

        def get_platform_name(self):
            return self.platform_meta.name

        def get_platform_id(self):
            return self.platform_meta.id

        def get_sender_id(self):
            return self.message_obj.sender.user_id

        def get_self_id(self):
            return self.message_obj.self_id

        def get_group_id(self):
            return self.message_obj.group_id

        def plain_result(self, text):
            return text

    fake_filter = types.SimpleNamespace(
        permission_type=decorator,
        command=decorator,
        PermissionType=types.SimpleNamespace(ADMIN="ADMIN"),
        on_llm_request=decorator,
        on_llm_response=decorator,
        on_llm_tool_respond=decorator,
    )
    for name in (
        "astrbot",
        "astrbot.core",
        "astrbot.core.platform",
        "astrbot.core.platform.sources",
        "astrbot.core.platform.sources.aiocqhttp",
    ):
        module(name)
    module("astrbot.api", AstrBotConfig=dict, logger=logging.getLogger("fishworld-test"))
    module("astrbot.api.event", AstrMessageEvent=FakeEvent, MessageChain=Chain, filter=fake_filter)
    module("astrbot.api.message_components", Plain=Plain)
    module(
        "astrbot.api.platform",
        AstrBotMessage=Message,
        MessageMember=Member,
        MessageType=MessageType,
    )
    module("astrbot.api.star", Context=object, Star=Star, StarTools=object, register=decorator)
    module(
        "astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
        AiocqhttpMessageEvent=FakeEvent,
    )
    module("aiocqhttp", Event=dict)
    return FakeEvent


FakeEvent = install_stubs()
main = importlib.import_module("astrbot_plugin_fishworld.main")
core = importlib.import_module("astrbot_plugin_fishworld.core")
NOW = 1794888000.0
WATCH_KEY = "fwr_offline_test_key"


def item(kind="message", text="收到新消息", sender="小蓝#1", timestamp=NOW * 1000):
    return {"kind": kind, "t": timestamp, "time": "12:00", "from": sender, "text": text}


def binding(group=False, isolated=False):
    sid = "123_789" if isolated else "789" if group else "123"
    kind = "GroupMessage" if group else "FriendMessage"
    return {
        "platform_id": "napcat-main",
        "umo": f"napcat-main:{kind}:{sid}",
        "session_id": sid,
        "message_type": kind,
        "sender_id": "123",
        "self_id": "456",
        "group_id": "789" if group else "",
    }


def snapshot(asleep=False, *, pose="stand", where=None, doing=None):
    return {
        "fish": "测试鱼#1",
        "stage": "live",
        "figure": {"asleep": asleep, "pose": pose},
        "where": {"kind": "home", "text": "家"} if where is None else where,
        "doing": doing,
        "body": {"text": "精神不错", "ill": None},
    }


class Tools:
    def __init__(self, tools):
        self.tools = tools

    def get_tool(self, name):
        return next((tool for tool in self.tools if tool.name == name), None)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.store = core.StateStore(self.path, NOW)
        self.store.use_key(WATCH_KEY, NOW)
        self.cfg = main.settings({"watch_key": WATCH_KEY})

    def receive(self, *items):
        if not self.store.data["cursor"]:
            self.store.ingest({"cursor": "1"}, self.cfg["event_kinds"], NOW)
        return self.store.ingest(
            {"cursor": "2", "items": list(items)}, self.cfg["event_kinds"], NOW
        )

    def test_first_fetch_only_baseline(self):
        self.assertEqual(
            self.store.ingest({"cursor": "1", "items": [item()]}, set(core.KINDS), NOW), 0
        )
        self.assertFalse(self.store.data["pending"])

    def test_cursor_and_outbox_survive_restart(self):
        self.receive(item())
        restored = core.StateStore(self.path, NOW)
        self.assertEqual(restored.data["cursor"], "2")
        self.assertEqual(len(restored.data["pending"]), 1)

    def test_dedup_within_batch_and_across_restart(self):
        self.assertEqual(self.receive(item(), item()), 1)
        self.store.consume(self.store.data["pending"][:])
        self.store.save()
        self.store = core.StateStore(self.path, NOW)
        self.assertEqual(self.receive(item()), 0)

    def test_same_sender_new_content_not_lost(self):
        self.assertEqual(self.receive(item(), item(text="收到两条新消息")), 2)

    def test_unknown_and_disabled_kinds_ignored(self):
        self.assertEqual(self.receive(item("arrive"), item("future_kind"), item()), 1)

    def test_malformed_response_does_not_advance_cursor(self):
        self.receive(item())
        for payload in (
            {"cursor": "3", "items": {}},
            {"items": []},
            {"cursor": "3", "items": [{}]},
        ):
            with self.assertRaises(ValueError):
                self.store.ingest(payload, self.cfg["event_kinds"], NOW)
            self.assertEqual(self.store.data["cursor"], "2")

    def test_notification_and_wake_separate(self):
        self.receive(item("note"), item("message"))
        agent, notify = self.store.choose(self.cfg, NOW)
        self.assertEqual([x["item"]["kind"] for x in agent], ["message"])
        self.assertEqual([x["item"]["kind"] for x in notify], ["note"])

    def test_new_kinds_and_expired_tasks_do_not_unnecessarily_wake(self):
        self.cfg["event_kinds"].add("cards")
        self.cfg["wake_kinds"].update(("cards", "like", "feed"))
        self.receive(
            item("like"), item("feed"), item("cards"), item("work", timestamp=(NOW - 200) * 1000)
        )
        for cards_ready in (False, True):
            agent, notify = self.store.choose(self.cfg, NOW, cards_ready=cards_ready)
            self.assertEqual([r["item"]["kind"] for r in agent], ["cards"] if cards_ready else [])
            self.assertTrue({"like", "feed", "work"} <= {r["item"]["kind"] for r in notify})

    def test_quota_falls_back_to_plain_notification(self):
        self.receive(item())
        self.store.data["wake_count"] = 48
        agent, notify = self.store.choose(self.cfg, NOW)
        self.assertEqual((len(agent), len(notify)), (0, 1))

    def test_talk_cooldown_defers_without_losing(self):
        self.receive(item("talk"), item("message"))
        self.store.data["talk_at"]["小蓝#1"] = NOW
        agent, _ = self.store.choose(self.cfg, NOW + 299)
        self.assertEqual(len(agent), 1)
        agent, _ = self.store.choose(self.cfg, NOW + 300)
        self.assertEqual(len(agent), 2)

    def test_quiet_cross_midnight_boundaries(self):
        from datetime import datetime

        def at(value):
            return datetime.fromisoformat("2026-11-17T" + value + ":00+08:00").timestamp()

        for hour, expected in (
            ("23:00", True),
            ("06:59", True),
            ("07:00", False),
            ("22:59", False),
        ):
            self.assertEqual(core.in_quiet_hours("23:00", "07:00", at(hour)), expected)
        self.assertFalse(core.in_quiet_hours("", "", NOW))
        for start, end in (("25:00", "07:00"), ("23:00", ""), ("07:00", "07:00")):
            with self.assertRaises(ValueError):
                core.in_quiet_hours(start, end, NOW)

    def test_quiet_retains_pending(self):
        self.receive(item())
        local = core.datetime.fromtimestamp(NOW, core.BEIJING)
        self.cfg.update(
            quiet_start=f"{local.hour:02d}:00", quiet_end=f"{(local.hour + 1) % 24:02d}:00"
        )
        self.assertEqual(self.store.choose(self.cfg, NOW), ([], []))
        self.assertEqual(len(self.store.data["pending"]), 1)

    def test_new_day_resets_counts_in_beijing(self):
        self.store.data.update(wake_count=48, idle_count=12)
        self.store.roll_day(NOW + 86400)
        self.assertEqual(self.store.data["wake_count"], 0)
        self.assertEqual(self.store.data["idle_count"], 0)

    def test_key_rotation_clears_other_account_data(self):
        self.store.data["binding"] = binding()
        self.receive(item())
        self.store.use_key("fwr_another_account", NOW)
        self.assertFalse(self.store.data["pending"])
        self.assertEqual(self.store.data["cursor"], "")
        self.assertEqual(self.store.data["binding"], binding())
        self.assertNotIn(WATCH_KEY, self.path.read_text("utf-8"))

    def test_corrupt_state_rejected(self):
        self.path.write_text('{"version": 99}', "utf-8")
        with self.assertRaises(ValueError):
            core.StateStore(self.path, NOW)

    def test_queue_capacity_does_not_advance_cursor(self):
        self.receive(item())
        self.store.data["pending"] *= core.MAX_PENDING
        with self.assertRaises(ValueError):
            self.receive(item(text="new"))
        self.assertEqual(self.store.data["cursor"], "2")

    def test_configuration_validation(self):
        self.assertEqual(main.settings({"interval_seconds": 1})["interval_seconds"], 15)
        with self.assertRaises(ValueError):
            main.settings({"mode": "typo"})
        with self.assertRaises(ValueError):
            main.settings({"event_kinds": "message"})


class Adapter:
    def __init__(self):
        self.events = []
        self.raise_on_commit = False
        self.on_commit = None

    def meta(self):
        return types.SimpleNamespace(id="napcat-main", name="aiocqhttp")

    def get_client(self):
        return self

    def commit_event(self, event):
        if self.on_commit:
            self.on_commit(event)
        if self.raise_on_commit:
            raise RuntimeError("queue unavailable")
        self.events.append(event)


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.adapter = Adapter()
        self.sent = []
        self.send_success = True

        async def send(umo, chain):
            if self.send_success:
                self.sent.append((umo, chain.text))
            return self.send_success

        context = types.SimpleNamespace(
            platform_manager=types.SimpleNamespace(get_insts=lambda: [self.adapter]),
            send_message=send,
        )
        self.plugin = main.FishWorldPlugin(context, {"watch_key": WATCH_KEY})
        self.plugin.path = self.path
        self.plugin.store = core.StateStore(self.path, NOW)
        self.plugin.store.use_key(WATCH_KEY, NOW)
        self.plugin.store.data["binding"] = binding()
        self.cfg = main.settings(self.plugin.config)
        self.plugin.store.ingest({"cursor": "1"}, self.cfg["event_kinds"], NOW)

    def receive(self, kind="message"):
        self.plugin.store.ingest(
            {"cursor": "2", "items": [item(kind)]}, self.cfg["event_kinds"], NOW
        )

    async def test_agent_submission_persisted_before_queue(self):
        self.receive()

        def committed(event):
            saved = core.StateStore(self.path, NOW).data
            self.assertFalse(saved["pending"])
            self.assertEqual(saved["wake_count"], 1)
            self.assertEqual(saved["busy"]["id"], event.get_extra(main.MARKER))

        self.adapter.on_commit = committed
        await self.plugin._deliver(self.cfg, NOW)
        event = self.adapter.events[0]
        self.assertTrue(event.is_wake)
        self.assertTrue(event.is_at_or_wake_command)
        self.assertEqual(event.unified_msg_origin, binding()["umo"])
        self.assertEqual(event.message_obj.raw_message["self_id"], 456)
        self.assertIn("world_now", event.message_str)

    async def test_failed_submission_restores_outbox_and_quota(self):
        self.receive()
        self.adapter.raise_on_commit = True
        await self.plugin._deliver(self.cfg, NOW)
        saved = core.StateStore(self.path, NOW).data
        self.assertEqual(len(saved["pending"]), 1)
        self.assertEqual(saved["wake_count"], 0)
        self.assertIsNone(saved["busy"])

    async def test_failed_qq_delivery_preserves_event(self):
        self.receive("note")
        self.send_success = False
        await self.plugin._deliver(self.cfg, NOW)
        self.assertEqual(len(self.plugin.store.data["pending"]), 1)
        self.assertFalse(self.adapter.events)

    async def test_plain_notification_does_not_call_model(self):
        self.receive("note")
        await self.plugin._deliver(self.cfg, NOW)
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.adapter.events)
        self.assertFalse(self.plugin.store.data["pending"])

    async def test_group_isolation_binding_preserved(self):
        self.plugin.store.data["binding"] = binding(group=True, isolated=True)
        self.receive()
        await self.plugin._deliver(self.cfg, NOW)
        event = self.adapter.events[0]
        self.assertEqual(event.session_id, "123_789")
        self.assertEqual(event.get_group_id(), "789")
        self.assertEqual(event.get_sender_id(), "123")

    async def test_busy_prevents_second_wake_and_final_response_clears(self):
        self.receive()
        await self.plugin._deliver(self.cfg, NOW)
        event = self.adapter.events[0]
        await self.plugin.on_request(event, None)
        await self.plugin.on_response(event, types.SimpleNamespace(tools_call_name=["world_now"]))
        self.assertIsNotNone(self.plugin.store.data["busy"])
        await self.plugin._deliver(self.cfg, NOW + 40)
        self.assertEqual(len(self.adapter.events), 1)
        await self.plugin.on_response(event, types.SimpleNamespace(tools_call_name=[]))
        self.assertIsNone(self.plugin.store.data["busy"])

    async def test_idle_disabled_by_default_then_respects_quota(self):
        self.assertFalse(self.plugin._idle_due(self.cfg, NOW + 1800))
        self.cfg["idle_minutes"] = 20
        self.assertFalse(self.plugin._idle_due(self.cfg, NOW + 1199))
        self.assertTrue(self.plugin._idle_due(self.cfg, NOW + 1200))
        for recent in (
            None,
            snapshot(True),
            snapshot(where={"kind": "trip"}),
            snapshot(doing={"until": NOW + 1500}),
        ):
            self.plugin.snapshot, self.plugin.snapshot_at = recent, NOW + 1200
            await self.plugin._deliver(self.cfg, NOW + 1200)
            self.assertFalse(self.adapter.events)
        self.plugin.snapshot = snapshot()
        await self.plugin._deliver(self.cfg, NOW + 1200)
        self.assertEqual(self.plugin.store.data["idle_count"], 1)
        self.assertIn("闲时", self.adapter.events[0].message_str)
        self.plugin.store.data["idle_count"] = 12
        self.assertFalse(self.plugin._idle_due(self.cfg, NOW + 3600))

    async def test_storage_failure_prevents_queue_submission(self):
        self.receive()
        with patch.object(self.plugin.store, "save", side_effect=OSError("disk full")):
            with self.assertRaises(RuntimeError):
                await self.plugin._deliver(self.cfg, NOW)
        self.assertTrue(self.plugin.storage_error)
        self.assertFalse(self.adapter.events)

    async def test_model_timeout_never_replays_consumed_batch(self):
        self.receive()
        await self.plugin._deliver(self.cfg, NOW)
        self.plugin.next_fetch = NOW + 99999
        with patch.object(main.time, "time", return_value=NOW + 601):
            await self.plugin.check()
        self.assertEqual(len(self.adapter.events), 1)
        self.assertIsNone(self.plugin.store.data["busy"])
        self.assertIn("未知", self.plugin.store.data["last_delivery"])

    async def test_pause_prevents_fetch_and_delivery(self):
        self.receive()
        self.plugin.store.data["paused"] = True
        await self.plugin.check()
        self.assertFalse(self.adapter.events)

    async def test_paused_busy_can_expire_without_resuming_poll(self):
        self.receive()
        await self.plugin._deliver(self.cfg, NOW)
        self.plugin.store.data["paused"] = True
        with patch.object(main.time, "time", return_value=NOW + 601):
            await self.plugin.check()
        self.assertIsNone(self.plugin.store.data["busy"])
        self.assertEqual(len(self.adapter.events), 1)

    async def test_sleep_restart_retry_and_awake_delivery_keep_original_cursor(self):
        received = []
        phase = {"mode": "sleep", "now": NOW}

        async def handler(request):
            self.assertEqual(request.headers["Authorization"], "Bearer " + WATCH_KEY)
            received.append((request.path, request.query.get("cursor")))
            if request.path == "/notify":
                return web.json_response({"cursor": "end", "items": [item("like")]})
            if phase["mode"] == "limited":
                stamp = core.datetime.fromtimestamp(phase["now"], core.timezone.utc)
                return web.Response(
                    status=429,
                    headers={
                        "Retry-After": format_datetime(stamp + timedelta(seconds=900), usegmt=True)
                    },
                )
            if phase["mode"] == "unknown":
                return web.json_response({"fish": "测试鱼#1", "figure": {"pose": "doze"}})
            sleeping = phase["mode"] == "sleep"
            return web.json_response(snapshot(sleeping, pose="sleep" if sleeping else "stand"))

        app = web.Application()
        app.router.add_get("/{path}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

        async with aiohttp.ClientSession() as session:
            self.plugin.session = session
            with (
                patch.object(main, "API", base + "/notify"),
                patch.object(main, "STATE_API", base + "/state"),
                patch.object(main.time, "time", side_effect=lambda: phase["now"]),
            ):
                # Detect sleep before touching notify or starting the model.
                await self.plugin.check()
                self.assertTrue(self.plugin.store.data["sleeping"])
                phase["now"] += 60
                await self.plugin.check()
                self.assertEqual(received, [("/state", None)])
                self.assertFalse(self.adapter.events)
                self.assertFalse(self.sent)
                self.assertEqual(self.plugin.store.data["cursor"], "1")

                # Simulate a restart and continue holding the original cursor across errors.
                self.plugin.store = core.StateStore(self.path, phase["now"])
                self.plugin._forget_watch()
                phase["mode"] = "limited"
                await self.plugin.check()
                self.assertTrue(self.plugin.store.data["sleeping"])
                self.assertEqual(self.plugin.next_state, phase["now"] + 900)
                phase["now"] += 899
                await self.plugin.check()
                self.assertEqual(len(received), 2)
                phase.update(mode="unknown", now=phase["now"] + 1)
                await self.plugin.check()
                self.assertTrue(self.plugin.store.data["sleeping"])
                self.assertEqual(len(received), 3)

                phase.update(mode="awake", now=phase["now"] + 120)
                await self.plugin.check()
                self.assertEqual(received[-2:], [("/state", None), ("/notify", "1")])
                self.assertEqual(self.plugin.store.data["cursor"], "end")
                self.assertFalse(self.plugin.store.data["sleeping"])
                self.assertIn("[like]", self.sent[0][1])
                self.assertFalse(self.adapter.events)

    async def test_probe_allowlist_and_success_require_real_tool_result(self):
        now_tool = types.SimpleNamespace(name="world_now")
        change_tool = types.SimpleNamespace(name="world_say")
        full = Tools([now_tool, change_tool])
        self.plugin._dispatch(self.cfg, NOW, [], test=True)
        event = self.adapter.events[-1]
        request = types.SimpleNamespace(func_tool=full)
        await self.plugin.on_request(event, request)
        self.assertEqual(request.func_tool.tools, [now_tool])
        self.assertEqual(full.tools, [now_tool, change_tool])
        await self.plugin.on_response(event, types.SimpleNamespace(tools_call_name=["world_now"]))
        self.assertNotIn("调用成功", self.plugin._status())
        await self.plugin.on_tool_result(
            event, now_tool, {}, {"isError": True, "content": [{"type": "text", "text": "错误"}]}
        )
        self.assertIn("调用失败", self.plugin.last_tool_result)
        await self.plugin.on_tool_result(
            event, now_tool, {}, {"content": [{"type": "text", "text": "真实现状"}]}
        )
        await self.plugin.on_response(event, types.SimpleNamespace(tools_call_name=[]))
        self.assertIn("world_now 实际调用成功", self.plugin._status())
        self.assertNotIn("真实现状", self.path.read_text("utf-8"))

    async def test_card_wake_requires_fresh_structured_place(self):
        self.cfg["event_kinds"].add("cards")
        self.cfg["wake_kinds"].add("cards")
        self.receive("cards")
        where = {"kind": "place", "place": {"id": "fulai-cards", "name": "福来棋牌室"}}
        self.plugin.snapshot, self.plugin.snapshot_at = snapshot(where=where), NOW - 50
        await self.plugin._deliver(self.cfg, NOW)
        self.assertFalse(self.adapter.events)
        self.assertIn("[cards]", self.sent[0][1])
        self.plugin.store.ingest(
            {"cursor": "3", "items": [item("cards", "新一局轮到自己")]},
            self.cfg["event_kinds"],
            NOW,
        )
        self.plugin.snapshot_at = NOW
        await self.plugin._deliver(self.cfg, NOW)
        self.assertIn("新一局轮到自己", self.adapter.events[0].message_str)

    async def test_current_status_reads_cached_sleep_state_without_notify_or_model(self):
        _, event = self.plugin._make_event("admin command")
        event.extras.clear()
        self.plugin.snapshot, self.plugin.snapshot_at = snapshot(True, pose="sleep"), NOW
        self.plugin.store.data["sleeping"] = True
        with patch.object(main.time, "time", return_value=NOW):
            replies = [r async for r in self.plugin.command(event, "现状")]
            status = self.plugin._status()
        self.assertIn("睡眠：正在睡觉", replies[0])
        self.assertIn("睡眠暂停中", status)
        self.assertEqual(self.plugin.store.data["cursor"], "1")
        self.assertFalse(self.adapter.events)

    async def test_unmounted_session_tool_requeues_notification_instead_of_world_action(self):
        self.receive()
        await self.plugin._deliver(self.cfg, NOW)
        event = self.adapter.events[0]
        request = types.SimpleNamespace(func_tool=Tools([types.SimpleNamespace(name="world_say")]))
        with patch.object(main.time, "time", return_value=NOW):
            await self.plugin.on_request(event, request)
        self.assertIsNone(request.func_tool)
        await self.plugin.on_response(event, types.SimpleNamespace(tools_call_name=[]))
        await self.plugin._deliver(self.cfg, NOW + 1)
        self.assertEqual(len(self.adapter.events), 1)
        self.assertIn("[message]", self.sent[0][1])
        self.assertEqual(self.plugin.store.data["pending"], [])

    async def test_diary_only_for_bound_owner_in_private_and_returns_plain_text(self):
        calls = []

        async def call(ctx, **kwargs):
            calls.append((ctx.context.event.get_sender_id(), kwargs))
            return {"content": [{"type": "text", "text": "10:01 看窗外：玻璃上的手印"}]}

        history = types.SimpleNamespace(
            name="world_history",
            mcp_server_name="fish-world",
            mcp_tool=types.SimpleNamespace(name="world_history"),
            parameters={"properties": {"date": {"type": "string"}, "limit": {"type": "integer"}}},
            call=call,
        )
        self.plugin.context.get_llm_tool_manager = lambda: types.SimpleNamespace(
            get_full_tool_set=lambda: Tools([history])
        )
        _, private = self.plugin._make_event("private")
        private.extras.clear()
        self.plugin.store.data["binding"] = binding(group=True)
        _, group = self.plugin._make_event("group")
        group.extras.clear()
        for event in (group, private):
            if event is private:
                private.message_obj.sender.user_id = "777"
            reply = [r async for r in self.plugin.command(event, "今日日记")]
            self.assertIn("私聊", reply[0])
        self.assertFalse(calls)
        private.message_obj.sender.user_id = "123"
        with patch.object(main.time, "time", return_value=NOW):
            reply = [r async for r in self.plugin.command(private, "今日日记")]
        self.assertIn("玻璃上的手印", reply[0])
        self.assertEqual(calls, [("123", {"date": core.day_at(NOW), "limit": 12})])
        self.assertFalse(self.adapter.events)
        self.assertNotIn("手印", self.path.read_text("utf-8"))

    async def test_missing_platform_retains_pending(self):
        self.receive()
        self.plugin.context.platform_manager.get_insts = lambda: []
        await self.plugin._deliver(self.cfg, NOW)
        self.assertEqual(len(self.plugin.store.data["pending"]), 1)
        self.assertEqual(self.plugin.store.data["wake_count"], 0)

    async def test_reject_mcp_operation_key(self):
        self.plugin.config["watch_key"] = "fw_not_for_polling"
        await self.plugin.check()
        self.assertIn("观看密钥", self.plugin.last_error)

    async def test_local_http_auth_cursor_retry_and_invalid_payload(self):
        requests = []
        status = {"code": 200, "payload": {"cursor": "3", "items": [item()]}}

        async def handler(request):
            requests.append((request.headers.get("Authorization"), request.query.get("cursor")))
            if status["code"] == 429:
                return web.Response(status=429, headers={"Retry-After": "120"}, text=WATCH_KEY)
            if status["code"] == 302:
                return web.Response(status=302, headers={"Location": "/elsewhere"})
            return web.json_response(status["payload"])

        app = web.Application()
        app.router.add_get("/notify", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        async with aiohttp.ClientSession() as session:
            self.plugin.session = session
            with patch.object(main, "API", f"http://127.0.0.1:{port}/notify"):
                await self.plugin._fetch(self.cfg, NOW)
                self.assertEqual(requests[0], ("Bearer " + WATCH_KEY, "1"))
                self.assertEqual(self.plugin.store.data["cursor"], "3")
                self.assertEqual(len(self.plugin.store.data["pending"]), 1)
                status["code"] = 429
                with self.assertLogs("fishworld-test", level="WARNING") as logs:
                    await self.plugin._fetch(self.cfg, NOW + 30)
                self.assertGreaterEqual(self.plugin.next_fetch, NOW + 150)
                self.assertNotIn(WATCH_KEY, "".join(logs.output))
                self.assertNotIn(WATCH_KEY, self.plugin.last_error)
                status.update(code=200, payload={"cursor": "4", "items": [{}]})
                await self.plugin._fetch(self.cfg, NOW + 150)
                self.assertEqual(self.plugin.store.data["cursor"], "3")
                status["code"] = 302
                count = len(requests)
                await self.plugin._fetch(self.cfg, NOW + 500)
                self.assertEqual(len(requests), count + 1)
                self.assertIn("302", self.plugin.last_error)

    async def test_no_duplicate_poll_on_concurrent_checks(self):
        calls = []

        async def fetch(cfg, now):
            calls.append(now)
            self.plugin.next_fetch = now + 30
            await asyncio.sleep(0.01)

        with patch.object(self.plugin, "_fetch", side_effect=fetch):
            await asyncio.gather(self.plugin.check(), self.plugin.check())
        self.assertEqual(len(calls), 1)

    async def test_reset_backs_up_and_preserves_daily_quota(self):
        self.receive()
        self.plugin.store.data["wake_count"] = 17
        self.plugin.store.save()
        _, event = self.plugin._make_event("admin command")
        event.extras.clear()
        replies = [reply async for reply in self.plugin.command(event, "重置确认")]
        self.assertIn("备份", replies[0])
        self.assertEqual(self.plugin.store.data["wake_count"], 17)
        self.assertFalse(self.plugin.store.data["pending"])
        self.assertEqual(len(list(self.path.parent.glob("state.backup-*.json"))), 1)


if __name__ == "__main__":
    unittest.main()
