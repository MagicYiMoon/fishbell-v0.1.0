from __future__ import annotations

import asyncio
from types import SimpleNamespace

from .watch import plain_text


class HistoryError(ValueError):
    pass


def _read(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def tool_text(result, limit: int = 3000) -> str:
    if result is None or _read(result, "isError", False):
        return ""
    content = _read(result, "content", ())
    if not isinstance(content, (list, tuple)):
        return ""
    text = "\n".join(
        block[:limit]
        for item in content[:12]
        if _read(item, "type") == "text" and isinstance(block := _read(item, "text"), str)
    ).strip()
    if text.startswith(("[E_", "Error:", "error:")):
        return ""
    return plain_text(text, limit)


def request_tool(request, name: str):
    toolset = getattr(request, "func_tool", None)
    if toolset is None or not hasattr(toolset, "get_tool"):
        return None
    tool = toolset.get_tool(name)
    return tool if tool and getattr(tool, "active", True) else None


def is_mcp_tool(tool, name: str, server: str) -> bool:
    raw = getattr(tool, "_wrapped", tool)  # Keep the permission-guarded tool for calls.
    if (
        tool is None
        or not getattr(tool, "active", True)
        or getattr(raw, "mcp_server_name", None) != server
        or _read(getattr(raw, "mcp_tool", None), "name") != name
    ):
        return False
    return True


def mounted_tool(context, name: str, server: str):
    manager = context.get_llm_tool_manager()
    tool = manager.get_full_tool_set().get_tool(name)
    return tool if is_mcp_tool(tool, name, server) else None


async def read_diary(context, event, server: str, date: str) -> str:
    tool = mounted_tool(context, "world_history", server)
    if tool is None:
        raise HistoryError("找不到已启用的鱼世界 world_history；请检查 MCP 名称和连接。")
    schema = getattr(tool, "parameters", None)
    props = schema.get("properties", {}) if isinstance(schema, dict) else {}
    if not isinstance(props, dict) or _read(props.get("date"), "type") != "string":
        raise HistoryError("当前 world_history 未提供按日期查询，无法读取今日日记。")
    args: dict[str, str | int] = {"date": date}
    limit = props.get("limit")
    if _read(limit, "type") == "integer":
        low, high = _read(limit, "minimum", 1), _read(limit, "maximum", 30)
        if type(low) is int and type(high) is int and 1 <= low <= high <= 100:
            args["limit"] = min(max(12, low), high)
    required = schema.get("required", ())
    if not isinstance(required, (list, tuple)) or any(k not in args for k in required):
        raise HistoryError("当前 world_history 参数与只读日记查询不兼容。")
    # MCPTool only needs the timeout; AstrBot's newer permission guard also needs the event.
    wrapper = SimpleNamespace(context=SimpleNamespace(event=event), tool_call_timeout=18)
    result = await asyncio.wait_for(tool.call(wrapper, **args), timeout=20)
    text = tool_text(result)
    if not text:
        raise HistoryError("world_history 没有成功返回可读文本，请检查 MCP 后重试。")
    return f"【今日日记 · {date} 北京时间】\n{text}"
