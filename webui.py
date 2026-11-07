"""WebUI 仪表盘（只读）：REST 端点注册与数据组装。

从 main.py 平移而来。AstrBot 插件页面机制：context.register_web_api 注册带
WebUI 登录鉴权的 REST 端点；pages/dashboard/ 下的前端经
window.AstrBotPluginPage Bridge 调用（apiGet("dashboard/overview")）。
首版只读：解绑/改配置仍走聊天，验证码同意链不在网页上复制。
任何响应都不得包含 cookie 值（有测试断言）。
"""

import logging
import time

logger = logging.getLogger("astrbot.plugin.dorm_electric")

# 与 main.PLUGIN_NAME 保持一致；经 getattr 兜底，插件实例没有该属性时用常量。
_PLUGIN_NAME = "astrbot_plugin_dorm_electric"


def _pname(plugin) -> str:
    return str(getattr(plugin, "plugin_name", "") or _PLUGIN_NAME)


def register_dashboard(plugin) -> None:
    reg = getattr(plugin.context, "register_web_api", None)
    if not callable(reg):
        logger.info("[%s] 宿主不支持 register_web_api，仪表盘端点未注册", _pname(plugin))
        return
    base = f"/{_pname(plugin)}/dashboard"
    try:
        reg(f"{base}/overview", plugin._web_overview, ["GET"], "电费仪表盘概览")
        reg(f"{base}/history", plugin._web_history, ["GET"], "电费仪表盘历史")
    except Exception as e:
        logger.warning("[%s] 仪表盘端点注册失败：%r", _pname(plugin), e)


def binding_item(plugin, umo: str, binding: dict) -> dict:
    """单个绑定的仪表盘数据（纯数据，无任何凭证字段）。"""
    fees = {}
    for kind in ("ac", "elec"):
        history = (binding.get("history_by_fee") or {}).get(kind) or []
        latest = history[-1] if history else None
        try:
            latest_value = float(latest["v"]) if latest else None
        except (TypeError, ValueError, KeyError):
            latest_value = None
        stats = plugin._history_stats(history)
        warn, critical = plugin._effective_thresholds(binding, kind)
        fees[kind] = {
            "name": plugin._fee_name(kind),
            "unit": (latest or {}).get("u", "度" if kind == "ac" else "元"),
            "latest_value": latest_value,
            "latest_time": float(latest["t"]) if latest else None,
            "warn": warn,
            "critical": critical,
            "custom": bool(
                isinstance(binding.get("thresholds"), dict)
                and isinstance(binding["thresholds"].get(kind), dict)
            ),
            "per_day": stats["per_day"] if stats else 0.0,
            "recharged_24h": stats["recharged_24h"] if stats else 0.0,
            "points": len(history),
        }
    return {
        "umo": umo,
        "label": plugin._binding_label(binding),
        "fees": fees,
        "alert_muted_until": float(plugin._alert_muted.get(umo, 0) or 0),
        "daily_muted_until": plugin._daily_muted_until(binding),
    }


def credential_state(plugin, bindings: dict) -> str:
    """从各会话最近一次查询结果推断学校凭证状态（不发额外请求）。

    - "expired"：有会话拿到 91001、无任何一路拿到余额，且连续确认计数达标
      （插件 CREDENTIAL_CONFIRM_STRIKES，跨轮询/保活周期）→ 凭证确定失效
    - "suspect"：见到 91001 但连续确认未达标 → 学校抖动观察期（学校发癫时
      会话即生即死、几分钟自愈），别渲染成红条吓用户去换凭证
    - "ok"：任一路拿到过余额 → 学校认了会话
    - "unknown"：没数据（没绑定/还没轮询过/网络失败）——不能断言，别吓用户

    cookie 已配置但学校不认（「已配置」≠「有效」）正是 v1.1.x 仪表盘的盲区。

    ⚠️ 判定口径必须与 main._credential_suspect_mode 一致：探测（轮询/保活）
    全关时没有连续确认机制，不能一直挂在「观察中」——直接判失效。
    """
    expired = False
    ok_seen = False
    raw_by = getattr(plugin, "_last_raw", {}) or {}
    for umo in bindings:
        wrap = raw_by.get(umo) or {}
        for result in (wrap.get("results") or {}).values():
            if getattr(result, "session_expired", False):
                expired = True
            elif getattr(result, "ok", False):
                ok_seen = True
    if expired and not ok_seen:
        confirmed = getattr(plugin, "_credential_dead_confirmed", None)
        if callable(confirmed) and confirmed():
            return "expired"
        suspect_mode = getattr(plugin, "_credential_suspect_mode", None)
        if callable(suspect_mode) and suspect_mode():
            return "suspect"
        return "expired"
    if ok_seen:
        return "ok"
    return "unknown"


async def overview(plugin) -> dict:
    """仪表盘概览：全部绑定 + 全局状态。"""
    try:
        bindings = (plugin.store.data.get("bindings", {}) if plugin.store else {}) or {}
        cookie = str(plugin.config.get("hjnu_cookie", "") or "")
        meta = dict(plugin.store.data.get("credential") or {}) if plugin.store else {}
        return {
            "success": True,
            "data": {
                "bindings": [
                    binding_item(plugin, umo, b) for umo, b in bindings.items()
                ],
                "cookie_ok": bool(cookie.strip()),
                "credential_state": credential_state(plugin, bindings),
                # 凭证生命周期埋点（纯时间戳，无凭证值）：
                # set_at=最后一次手动更新；last_ok_at/last_expired_at/
                # last_net_fail_at=学校最近一次认/拒绝/失联
                "credential_meta": meta,
                "poll_interval_minutes": plugin._cfg_int("poll_interval_minutes", 20),
                "keepalive_interval_minutes": plugin._cfg_int(
                    "keepalive_interval_minutes", 8
                ),
                "daily_time": str(plugin._cfg("daily_time", "08:00")),
                "daily_report": plugin._cfg_bool("daily_report", True),
                "server_time": time.time(),
            },
        }
    except Exception as e:
        # 不把内部异常细节透给网页端（可能带路径/配置信息），细节进日志
        logger.warning("[%s] dashboard/overview 处理失败：%r", _pname(plugin), e)
        return {"success": False, "message": "内部错误，请查看服务端日志"}


async def history(plugin) -> dict:
    """仪表盘历史：指定会话两个费种的日末快照序列（days 上限 60）。"""
    try:
        try:
            from quart import request
        except ImportError:
            return {"success": False, "message": "宿主 Web 框架不可用"}
        umo = str(request.args.get("umo", ""))
        try:
            days = int(request.args.get("days", 14))
        except (TypeError, ValueError):
            days = 14  # 乱传参数回默认值，别让 ValueError 落进「内部错误」
        days = max(1, min(60, days))
        binding = plugin.store.get_binding(umo) if plugin.store else None
        if not binding:
            return {"success": False, "message": "会话不存在或未绑定"}
        tz = plugin._resolve_tz(plugin._cfg("daily_timezone", "Asia/Shanghai"))
        series = {}
        for kind in ("ac", "elec"):
            hist = (binding.get("history_by_fee") or {}).get(kind) or []
            snaps = plugin._daily_snapshots(hist, tz, days)
            series[kind] = [
                {
                    "date": str(d),
                    "value": (float(rec["v"]) if rec else None),
                }
                for d, rec in snaps
            ]
        return {
            "success": True,
            "data": {
                "label": plugin._binding_label(binding),
                "days": days,
                "series": series,
            },
        }
    except Exception as e:
        # 同 overview：对外统一文案，细节进日志
        logger.warning("[%s] dashboard/history 处理失败：%r", _pname(plugin), e)
        return {"success": False, "message": "内部错误，请查看服务端日志"}
