"""凭证失效误报修复（v1.3.0）：Provider 层 91001 复验 + 连续确认计数。

背景：学校缴费系统夜里「发癫」时会把**还活着的会话**临时判成 91001
（实录：会话即生即死、几分钟后自愈）。旧实现是单次 91001 就发失效通知 +
置 credential_dead，用户被叫去重新提取凭证——纯白折腾。

两道防线：
1. Provider 层：91001 隔 EXPIRED_RECHECK_DELAY 秒原样重发一次，复验通过就
   当抖动处理（挡秒级抖动）。
2. main 层：连续 CREDENTIAL_CONFIRM_STRIKES 次（跨轮询/保活周期）才发通知、
   才置 credential_dead（挡分钟级抖动）；网络失败不推进计数，任一路余额清零。
"""

import asyncio
import json

import httpx
from astrbot_plugin_dorm_electric.providers import http_json
from astrbot_plugin_dorm_electric.providers.base import BalanceResult
from astrbot_plugin_dorm_electric.providers.http_json import (
    HjnuProvider,
    SessionExpiredError,
)
from conftest import make_plugin

OK_BALANCE = {"retcode": "0", "errmsg": "A-8-17房间剩余电量94.66度"}
EXPIRED = {"retcode": "91001", "errmsg": "会话已超时"}
ROOM_PARAMS = {
    "aid": "a1",
    "area": {"areaname": "校本部"},
    "building": {"building": "春雪楼2"},
    "floor": {"floor": "8层"},
    "room": {"room": "A-8-17", "roomid": 646},
}
UMO = "qq:private:1"


class _Store:
    def __init__(self, bindings=None):
        self.data = {"bindings": dict(bindings or {})}
        self.saved = 0

    def get_binding(self, umo):
        return self.data["bindings"].get(umo)

    def save(self, force=False):
        self.saved += 1


def _provider(script, monkeypatch):
    """按脚本回放响应；script 元素：(status, text)。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        action = script[len(calls)] if len(calls) < len(script) else script[-1]
        calls.append(str(request.url.path))
        status, text = action
        return httpx.Response(status, text=text)

    provider = HjnuProvider(
        base_url="http://school.test",
        query_path="/q",
        cookie="JSESSIONID=x",
        user_agent="ua",
        referer="http://school.test/",
        timeout=5,
        transport=httpx.MockTransport(handler),
    )

    async def _nosleep(_):
        return None

    monkeypatch.setattr(http_json.asyncio, "sleep", _nosleep)
    return provider, calls


# ================= Provider 层：91001 复验 =================


def test_query_room_91001_then_ok_is_treated_as_flap(monkeypatch):
    """第一次 91001、复验拿到余额 → 正常返回余额，不报凭证失效。"""
    provider, calls = _provider(
        [
            (200, json.dumps(EXPIRED)),
            (200, json.dumps(OK_BALANCE)),
        ],
        monkeypatch,
    )
    result = asyncio.run(provider.query_room(ROOM_PARAMS))
    assert result.ok is True
    assert result.value == 94.66
    assert result.session_expired is False
    assert len(calls) == 2  # 复验确实重发了一次


def test_query_room_91001_twice_is_expired(monkeypatch):
    """复验仍 91001 → 真失效（会话已失效）。"""
    provider, calls = _provider(
        [(200, json.dumps(EXPIRED)), (200, json.dumps(EXPIRED))],
        monkeypatch,
    )
    result = asyncio.run(provider.query_room(ROOM_PARAMS))
    assert result.ok is False
    assert result.session_expired is True
    assert len(calls) == 2


def test_list_areas_91001_then_ok_is_treated_as_flap(monkeypatch):
    """保活探测走的 list_areas 同样带复验（抖动不误判失效）。"""
    provider, calls = _provider(
        [
            (200, json.dumps(EXPIRED)),
            (200, json.dumps({"retcode": "0", "areatab": [{"area": "1"}]})),
        ],
        monkeypatch,
    )
    areas = asyncio.run(provider.list_areas("a1"))
    assert areas == [{"area": "1"}]
    assert len(calls) == 2


def test_list_areas_91001_twice_raises_session_expired(monkeypatch):
    provider, calls = _provider(
        [(200, json.dumps(EXPIRED)), (200, json.dumps(EXPIRED))],
        monkeypatch,
    )
    try:
        asyncio.run(provider.list_areas("a1"))
    except SessionExpiredError:
        pass
    else:
        raise AssertionError("复验仍 91001 时应抛 SessionExpiredError")
    assert len(calls) == 2


def test_recheck_delay_is_short(monkeypatch):
    """复验延迟是秒级常量（太长会拖慢用户查询）。"""
    assert 0 < http_json.EXPIRED_RECHECK_DELAY <= 5


# ================= main 层：连续确认计数 =================


def _plugin(store=None):
    store = store or _Store()
    return make_plugin(store=store), store


def test_strikes_increment_and_confirm():
    plugin, store = _plugin()
    assert plugin._expired_strikes() == 0
    assert plugin._credential_dead_confirmed() is False

    plugin._update_expired_strikes(expired=True)
    assert plugin._expired_strikes() == 1
    assert plugin._credential_dead_confirmed() is False

    plugin._update_expired_strikes(expired=True)
    assert plugin._expired_strikes() == 2
    assert plugin._credential_dead_confirmed() is True
    assert store.data["credential"]["expired_strikes"] == 2


def test_strikes_cleared_by_ok():
    plugin, _store = _plugin()
    plugin._update_expired_strikes(expired=True)
    plugin._update_expired_strikes(expired=True)
    plugin._update_expired_strikes(ok=True)
    assert plugin._expired_strikes() == 0
    assert plugin._credential_dead_confirmed() is False


def test_network_failure_does_not_touch_strikes():
    """网络失败既不推进也不清除观察进度（ok=False, expired=False）。"""
    plugin, _store = _plugin()
    plugin._update_expired_strikes(expired=True)
    plugin._update_expired_strikes(ok=False, expired=False)
    assert plugin._expired_strikes() == 1


def test_strikes_survive_restart():
    """计数持久化在 store.data["credential"]，重启不清零。"""
    plugin, store = _plugin()
    plugin._update_expired_strikes(expired=True)
    plugin2 = make_plugin(store=store)
    assert plugin2._expired_strikes() == 1


def test_strikes_without_store_is_noop():
    plugin = make_plugin(store=None)
    assert plugin._update_expired_strikes(expired=True) == 0
    assert plugin._expired_strikes() == 0


# ================= 手动换凭证清零 =================


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


def test_manual_credential_update_resets_strikes():
    """用户手工换新凭证 → 旧凭证攒下的观察计数清零。"""
    store = _Store()
    plugin = make_plugin(store=store)
    plugin._update_expired_strikes(expired=True)
    assert plugin._expired_strikes() == 1

    out = "".join(
        asyncio.run(_collect(plugin.cmd_credential(_Ev(), "JSESSIONID=NEW")))
    )
    assert "凭证已保存" in out
    assert plugin._expired_strikes() == 0


# ================= 交互查询：单次 91001 的呈现 =================


def test_expired_result_renders_as_expired():
    """复验后仍失效 → 明确提示凭证失效（用户据此换凭证）。"""
    result = BalanceResult(ok=False, value=None, raw="会话已超时", session_expired=True)
    assert result.session_expired is True
