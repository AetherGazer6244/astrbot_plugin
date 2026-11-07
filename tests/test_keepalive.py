"""凭证保活探测与生命周期埋点（v1.2.2）。

背景：凭证（JSESSIONID）是全局配置，此前失效完全无痕——不记时间、
不报警、失效通知 24h 去重，用户只能事后猜「这张票到底活了多久」。
v1.2.2 三件事：
1. 独立保活任务 _keepalive_tick：与余额轮询解耦，没有绑定会话也照常
   打卡；用 queryElecArea（只需 aid，响应最轻）探测凭证死活
2. 生命周期埋点 _touch_credential：set_at（cmd_credential 写）+
   last_ok_at / last_expired_at / last_net_fail_at（轮询与保活写）
3. 仪表盘 overview 暴露 credential_meta 与 keepalive_interval_minutes

判定口径与 _sync_credential_state 一致：学校 5xx/网络全挂 ≠ 凭证失效
（学校夜里发癫时绝不能把「临时失联」误判成「凭证过期」害用户重铸）。
"""

import asyncio
import json
import time

from astrbot_plugin_dorm_electric.providers.base import BalanceResult, QueryError
from astrbot_plugin_dorm_electric.providers.http_json import SessionExpiredError
from conftest import make_plugin

UMO = "qq:private:1"
AID = "0030000000004301"
ELEC_AID = "0030000000014501"
_CFG = {"hjnu_cookie": "JSESSIONID=x", "fee_items": {AID: "空调费"}}


class _Ev:
    def __init__(self, umo=UMO, private=True, is_admin=True):
        self.unified_msg_origin = umo
        self._private = private
        self._is_admin = is_admin

    def is_private_chat(self):
        return self._private

    def is_admin(self):
        return self._is_admin

    def plain_result(self, text):
        return text


async def _collect(agen):
    return [item async for item in agen]


def _ok(value, unit="度"):
    return BalanceResult(
        ok=True, value=value, unit=unit, raw=f"余额 {value} {unit}", session_expired=False
    )


def _expired():
    return BalanceResult(
        ok=False, value=None, raw="会话已超时", session_expired=True
    )


class _Store:
    def __init__(self, bindings=None):
        self.data = {"bindings": dict(bindings or {})}
        self.saved = 0
        self.forced = 0

    def get_binding(self, umo):
        return self.data["bindings"].get(umo)

    def save(self, force=False):
        self.saved += 1
        if force:
            self.forced += 1

    @staticmethod
    def append_fee_history(binding, fee, value, unit, ts=None, keep_days=60):
        from astrbot_plugin_dorm_electric.storage import Store

        Store.append_fee_history(binding, fee, value, unit, ts=ts, keep_days=keep_days)


class _Areas:
    """保活探测打的是 list_areas；按需抛 SessionExpired/QueryError。"""

    def __init__(self, error=None):
        self._error = error
        self.calls = []

    async def list_areas(self, aid):
        self.calls.append(aid)
        if self._error is not None:
            raise self._error
        return [{"area": "1", "areaname": "校本部"}]


class _FetchProvider:
    """_poll_all 路径的假 provider（_fetch_entry 传 {"params": params}）。"""

    def __init__(self, results=None, missing=False):
        self._results = results or {}
        self._missing = missing

    async def fetch(self, binding):
        if self._missing:
            return None
        params = binding.get("params") or {}
        return self._results.get(params.get("aid"))


class _CookieProvider:
    def __init__(self):
        self.cookie = None

    def update_cookie(self, cookie):
        self.cookie = cookie


def _binding():
    return {
        "room_label": "校本部/春雪楼2/8层/A-8-17",
        "fees": {
            "ac": {"provider": "hjnu", "params": {"aid": AID}},
            "elec": {"provider": "hjnu", "params": {"aid": ELEC_AID}},
        },
        "history_by_fee": {"ac": [{"t": time.time(), "v": 50.0, "u": "度"}]},
    }


def _recorder():
    sent = []

    async def _send(umo, text):
        sent.append((umo, text))
        return True

    return sent, _send


# ================= _touch_credential =================


def test_touch_writes_only_requested_keys():
    store = _Store()
    plugin = make_plugin(store=store)
    plugin._touch_credential(ok=True)
    assert set(store.data["credential"]) == {"last_ok_at"}
    plugin._touch_credential(expired=True)
    plugin._touch_credential(net_fail=True)
    meta = store.data["credential"]
    assert {"last_ok_at", "last_expired_at", "last_net_fail_at"} <= set(meta)
    assert all(meta[k] > 0 for k in meta)
    assert store.saved >= 3


def test_touch_without_store_noop():
    plugin = make_plugin(store=None)
    plugin._touch_credential(ok=True)  # 不抛即可


# ================= _keepalive_tick =================


def test_keepalive_ok_touches_and_clears_dead_flag():
    store = _Store({UMO: _binding()})
    store.get_binding(UMO)["credential_dead"] = True
    provider = _Areas()
    plugin = make_plugin(config=_CFG, store=store, hjnu=provider)

    asyncio.run(plugin._keepalive_tick())

    assert provider.calls == [AID]
    assert store.data["credential"]["last_ok_at"] > 0
    assert "last_expired_at" not in store.data["credential"]
    # 凭证活了 → 各会话 credential_dead 清除（被压住的每日播报恢复）
    assert store.get_binding(UMO)["credential_dead"] is False


def test_keepalive_first_expiry_only_enters_observation():
    """第一次 91001：只进观察期，不标记失效、不发通知（学校发癫防误报）。"""
    store = _Store({UMO: _binding()})
    plugin = make_plugin(
        config=_CFG, store=store, hjnu=_Areas(SessionExpiredError("会话已超时"))
    )
    sent, _send = _recorder()
    plugin._send = _send

    asyncio.run(plugin._keepalive_tick())

    assert store.data["credential"]["last_expired_at"] > 0
    assert store.data["credential"]["expired_strikes"] == 1
    assert not store.get_binding(UMO).get("credential_dead")
    assert sent == []


def test_keepalive_expired_notifies_and_marks_dead():
    """连续第二次 91001 → 标记失效 + 发通知（失效期唯一的推送）。"""
    store = _Store({UMO: _binding()})
    plugin = make_plugin(
        config=_CFG, store=store, hjnu=_Areas(SessionExpiredError("会话已超时"))
    )
    sent, _send = _recorder()
    plugin._send = _send

    asyncio.run(plugin._keepalive_tick())  # 第一次：观察期
    asyncio.run(plugin._keepalive_tick())  # 第二次：确认失效

    assert store.data["credential"]["last_expired_at"] > 0
    assert store.get_binding(UMO)["credential_dead"] is True
    assert len(sent) == 1
    assert "凭证已失效" in sent[0][1]
    assert any(e["kind"] == "credential" for e in plugin._events)


def test_keepalive_net_fail_preserves_state():
    """学校 5xx/网络抖动：记失联时间戳，凭证状态一字不动、不发通知。"""
    store = _Store({UMO: _binding()})
    store.get_binding(UMO)["credential_dead"] = True
    plugin = make_plugin(
        config=_CFG, store=store, hjnu=_Areas(QueryError("接口返回 HTTP 502"))
    )
    sent, _send = _recorder()
    plugin._send = _send

    asyncio.run(plugin._keepalive_tick())

    meta = store.data["credential"]
    assert meta["last_net_fail_at"] > 0
    assert "last_ok_at" not in meta and "last_expired_at" not in meta
    assert store.get_binding(UMO)["credential_dead"] is True
    assert sent == []


def test_keepalive_multiple_bindings_all_synced():
    store = _Store({UMO: _binding(), "webchat:x": _binding()})
    store.get_binding("webchat:x")["credential_dead"] = True
    plugin = make_plugin(config=_CFG, store=store, hjnu=_Areas())

    asyncio.run(plugin._keepalive_tick())

    # _sync_credential_state 只在状态翻转时写键：原本就没失效的会话不写
    assert store.get_binding(UMO).get("credential_dead") is not True
    assert store.get_binding("webchat:x")["credential_dead"] is False


def test_keepalive_skipped_without_cookie():
    provider = _Areas()
    plugin = make_plugin(
        config={"fee_items": {AID: "空调费"}}, store=_Store(), hjnu=provider
    )
    asyncio.run(plugin._keepalive_tick())
    assert provider.calls == []  # 没配凭证，白打一次请求


def test_keepalive_skipped_without_aid():
    provider = _Areas()
    plugin = make_plugin(
        config={"hjnu_cookie": "JSESSIONID=x"}, store=_Store(), hjnu=provider
    )
    asyncio.run(plugin._keepalive_tick())
    assert provider.calls == []


def test_keepalive_skipped_without_store_or_provider():
    plugin = make_plugin(config=_CFG, store=None, hjnu=_Areas())
    asyncio.run(plugin._keepalive_tick())  # 不抛即可
    plugin2 = make_plugin(config=_CFG, store=_Store(), hjnu=None)
    asyncio.run(plugin2._keepalive_tick())  # 不抛即可


# ================= 轮询路径也写埋点 =================


def test_poll_ok_records_meta():
    store = _Store({UMO: _binding()})
    plugin = make_plugin(
        store=store,
        hjnu=_FetchProvider({AID: _ok(50.0), ELEC_AID: _ok(8.0, "元")}),
    )
    _sent, _send = _recorder()
    plugin._send = _send

    asyncio.run(plugin._poll_all())

    assert store.data["credential"]["last_ok_at"] > 0
    assert "last_expired_at" not in store.data["credential"]


def test_poll_all_expired_records_expired_meta():
    store = _Store({UMO: _binding()})
    plugin = make_plugin(
        store=store,
        hjnu=_FetchProvider({AID: _expired(), ELEC_AID: _expired()}),
    )
    _sent, _send = _recorder()
    plugin._send = _send

    asyncio.run(plugin._poll_all())

    assert store.data["credential"]["last_expired_at"] > 0
    assert "last_ok_at" not in store.data["credential"]


def test_poll_net_fail_records_net_fail_meta():
    """结果全 None（网络挂）→ 只记失联，不冒充失效。"""
    store = _Store({UMO: _binding()})
    plugin = make_plugin(store=store, hjnu=_FetchProvider(missing=True))
    _sent, _send = _recorder()
    plugin._send = _send

    asyncio.run(plugin._poll_all())

    meta = store.data["credential"]
    assert meta["last_net_fail_at"] > 0
    assert "last_ok_at" not in meta and "last_expired_at" not in meta


# ================= cmd_credential 记 set_at =================


def test_cmd_credential_records_set_at_with_force_save():
    store = _Store()
    provider = _CookieProvider()
    plugin = make_plugin(config={"hjnu_cookie": ""}, store=store, hjnu=provider)
    out = "".join(
        asyncio.run(_collect(plugin.cmd_credential(_Ev(), "JSESSIONID=NEW")))
    )
    assert "凭证已保存" in out
    assert store.data["credential"]["set_at"] > 0
    # 用户动作必须立刻落盘（不能进 60 秒去抖窗口）
    assert store.forced >= 1


# ================= 仪表盘暴露埋点 =================


def test_overview_exposes_credential_meta_and_keepalive():
    store = _Store()
    store.data["credential"] = {
        "set_at": time.time() - 3600,
        "last_ok_at": time.time(),
    }
    plugin = make_plugin(
        config={"hjnu_cookie": "JSESSIONID=SECRETVAL", "keepalive_interval_minutes": 8},
        store=store,
    )
    resp = asyncio.run(plugin._web_overview())
    data = resp["data"]
    assert data["credential_meta"]["set_at"] > 0
    assert data["keepalive_interval_minutes"] == 8
    # 埋点只含时间戳：整个响应不得出现凭证值（与 webui 安全约定一致）
    assert "SECRETVAL" not in json.dumps(resp, default=str)


def test_overview_credential_meta_defaults_to_empty():
    plugin = make_plugin(config={"hjnu_cookie": "JSESSIONID=x"}, store=_Store())
    resp = asyncio.run(plugin._web_overview())
    assert resp["data"]["credential_meta"] == {}
