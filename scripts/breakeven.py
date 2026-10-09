#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦回本 (mc-breakeven)：用你自己的历史点餐，算清楚【麦金卡】值不值得买。

只读：只调用硬编码白名单内的 MCP 工具；不会下单、不会取消、不会领券。
仅使用 Python 标准库（3.9+）。令牌只从环境变量 MCD_MCP_TOKEN 读取，绝不打印或写入。

# 测试专用（未公开）：环境变量 MCD_MCP_URL 可覆盖服务地址（默认 https://mcp.mcd.cn），
# 仅用于本地 mock 服务联调；MCB_BACKOFF_BASE 可缩短重试等待（秒）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BASE_URL = os.environ.get("MCD_MCP_URL", "https://mcp.mcd.cn")
TOKEN_ENV = "MCD_MCP_TOKEN"
MARKER = "## Original Response"

# 硬编码只读白名单：其他任何工具一律拒绝（在发起任何网络请求之前）。
ALLOWED_TOOLS = frozenset({
    "order-list",
    "query-meals",
    "query-meal-detail",
    "calculate-price",
    "query-nearby-stores",
    "query-my-account",
    "now-time-info",
})

FALLBACK_MEMBERSHIP = ("106", "870")
SLEEP_BETWEEN_CALLS = 0.15
MAX_RETRIES = 3

EXIT_OK, EXIT_ERROR, EXIT_NO_TOKEN, EXIT_AUTH = 0, 1, 2, 3


class ForbiddenToolError(Exception):
    """请求了不在只读白名单内的工具。"""


class AuthError(Exception):
    """401：令牌无效或过期。"""


class NetworkError(Exception):
    """网络层错误。"""


class McpError(Exception):
    def __init__(self, message: str, code: Any = None):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- 工具函数

def mask(token: str) -> str:
    """调试输出用的令牌掩码，不泄露内容。"""
    return "***" if token else "(empty)"


def parse_response(text: str) -> Dict[str, Any]:
    """MCP 文本：Markdown 前言 + `## Original Response` + 一个 JSON 对象。"""
    idx = text.find(MARKER)
    start = text.find("{", idx + len(MARKER) if idx >= 0 else 0)
    if start < 0:
        raise McpError("响应中未找到 JSON 数据")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        raise McpError("响应 JSON 解析失败")
    if not isinstance(obj, dict):
        raise McpError("响应格式异常")
    return obj


def _extract_body(raw: str) -> Dict[str, Any]:
    """响应体可能是纯 JSON，也可能是 SSE（data: ...）。"""
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    datas = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
    for d in reversed(datas):
        if d.startswith("{"):
            return json.loads(d)
    raise McpError("无法解析服务端响应")


def yuan(cents: Any) -> float:
    return round(float(cents) / 100.0, 2)


def fmt(v: Optional[float]) -> str:
    return "-" if v is None else "%.2f" % v


def get_token() -> str:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        sys.stderr.write(
            "未检测到环境变量 MCD_MCP_TOKEN。\n"
            "获取方式：打开 https://open.mcd.cn/mcp -> 登录 -> 控制台 -> 激活，获得你的 MCP Token。\n"
            "然后在终端设置（不要写进任何文件或仓库）：\n"
            "  macOS / Linux : export MCD_MCP_TOKEN=\"你的Token\"\n"
            "  Windows PowerShell : $env:MCD_MCP_TOKEN=\"你的Token\"\n"
            "只想先看看效果？运行： python3 scripts/breakeven.py --demo analyze （离线示例数据，无需 Token）\n"
        )
        sys.exit(EXIT_NO_TOKEN)
    return token


# ---------------------------------------------------------------- 客户端

class _BaseClient:
    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def _check_allowed(name: str) -> None:
        if name not in ALLOWED_TOOLS:
            raise ForbiddenToolError("工具 %s 不在只读白名单内，已拒绝" % name)

    def call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError


class McdClient(_BaseClient):
    def __init__(self, token: str, base_url: Optional[str] = None) -> None:
        _BaseClient.__init__(self)
        self._token = token
        self.base_url = base_url or BASE_URL
        self._id = 0

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": "Bearer " + self._token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        base = float(os.environ.get("MCB_BACKOFF_BASE", "1"))
        attempt = 0
        while True:
            req = urllib.request.Request(self.base_url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return _extract_body(resp.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise AuthError("令牌无效或已过期（401）")
                if e.code == 429:
                    if attempt >= MAX_RETRIES:
                        raise McpError("触发限流（429），重试 %d 次后放弃，请稍后再试" % MAX_RETRIES, 429)
                    time.sleep(base * (2 ** attempt))
                    attempt += 1
                    continue
                raise McpError("服务端返回 HTTP %d" % e.code, e.code)
            except (urllib.error.URLError, OSError) as e:
                raise NetworkError("网络错误：%s" % getattr(e, "reason", type(e).__name__))
            except ValueError:
                raise McpError("无法解析服务端响应")

    def call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        self._check_allowed(name)
        if self.calls:
            time.sleep(SLEEP_BETWEEN_CALLS)
        self.calls += 1
        self._id += 1
        body = self._post({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                           "params": {"name": name, "arguments": args}})
        if body.get("error"):
            err = body["error"]
            raise McpError(str(err.get("message", "MCP 错误")), err.get("code"))
        result = body.get("result") or {}
        content = result.get("content") or []
        if not content:
            raise McpError("响应缺少 content")
        obj = parse_response(content[0].get("text", ""))
        if obj.get("success") is False:
            raise McpError(str(obj.get("message", "调用失败")), obj.get("code"))
        return obj


class DemoClient(_BaseClient):
    """离线演示客户端：与 McdClient 接口一致，数据来自 demo_data（虚构）。"""

    def __init__(self, scenario: str = "default") -> None:
        _BaseClient.__init__(self)
        self.scenario = scenario

    def call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        self._check_allowed(name)
        import demo_data
        demo_data.SCENARIO = self.scenario
        self.calls += 1
        try:
            return demo_data.handle(name, args)
        except demo_data.DemoError as e:
            raise McpError(e.message, e.code)


# ---------------------------------------------------------------- 分析

def _parse_time(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _skip_reason(o: Dict[str, Any]) -> Optional[str]:
    status = str(o.get("orderStatus", ""))
    if "取消" in status or "cancel" in status.lower():
        return "订单已取消"
    if str(o.get("orderType")) == "2":
        return "麦乐送订单暂不支持回放（需要地址流程）"
    if str(o.get("orderType")) != "1":
        return "订单类型暂不支持回放"
    bt = str(o.get("beType"))
    if bt not in ("1", "5"):
        return "该就餐方式（beType=%s）暂不支持回放" % bt
    if not o.get("orderProductList"):
        return "订单无商品明细"
    return None


def _item_label(o: Dict[str, Any], limit: int = 3) -> str:
    parts = ["%s×%s" % (p.get("productName", p.get("productCode")), p.get("quantity", 1))
             for p in o.get("orderProductList", [])]
    s = "、".join(parts[:limit])
    return s + ("…等%d项" % len(parts) if len(parts) > limit else "")


def discover_card(client: _BaseClient, order: Dict[str, Any], warnings: List[str]) -> Dict[str, Any]:
    args = {"storeCode": order["storeCode"], "orderType": str(order["orderType"]),
            "beType": str(order["beType"])}
    if order.get("beCode"):
        args["beCode"] = order["beCode"]
    found = []  # type: List[tuple]
    try:
        meals = (client.call_tool("query-meals", args).get("data") or {}).get("meals") or {}
    except McpError as e:
        warnings.append("门店 %s 的菜单查询失败（%s），改用默认会员参数。" % (order.get("storeName", ""), e.message))
        meals = {}
    for m in meals.values():
        if "随单购" in str(m.get("discountType", "")) and m.get("canWithOrder"):
            wo = m.get("withOrder") or {}
            if wo.get("membershipCode") and wo.get("specId"):
                pair = (str(wo["membershipCode"]), str(wo["specId"]))
                if pair not in found:
                    found.append(pair)
    discovered = bool(found)
    if not found:
        found = [FALLBACK_MEMBERSHIP]
        warnings.append("门店 %s 未发现随单购麦金卡参数，已回退到默认值 %s/%s，结果仅供参考。"
                        % (order.get("storeName", ""), found[0][0], found[0][1]))
    elif len(found) > 1:
        warnings.append("门店 %s 发现多个随单购会员参数，已使用第一个（%s/%s）。"
                        % (order.get("storeName", ""), found[0][0], found[0][1]))
    return {"membershipCode": found[0][0], "specId": found[0][1], "discovered": discovered,
            "meals": meals}


def replay_order(client: _BaseClient, o: Dict[str, Any], card: Dict[str, Any]) -> Dict[str, Any]:
    items = [{"productCode": p["productCode"], "quantity": int(p.get("quantity", 1))}
             for p in o["orderProductList"]]
    base = {"storeCode": o["storeCode"], "orderType": str(o["orderType"]),
            "beType": str(o["beType"]), "items": items}
    if o.get("beCode"):
        base["beCode"] = o["beCode"]
    without = client.call_tool("calculate-price", dict(base))
    wargs = dict(base)
    wargs["withOrder"] = {"membershipCode": card["membershipCode"],
                          "membershipSpecId": card["specId"]}
    with_ = client.call_tool("calculate-price", wargs)
    codes = {i["productCode"] for i in items}
    wd, ld = without.get("data") or {}, with_.get("data") or {}
    fee_lines = [l for l in (ld.get("productList") or []) if l.get("productCode") not in codes]
    if not fee_lines:
        raise McpError("有卡结果中未找到卡费行")
    fee = max(int(l.get("subtotal", 0)) for l in fee_lines)
    p0, p1 = int(wd["price"]), int(ld["price"])
    enjoyed = wd.get("enjoyed")
    identity = None
    if isinstance(enjoyed, dict) and (enjoyed.get("promotionName") or enjoyed.get("realDiscount")):
        identity = {"promotionName": str(enjoyed.get("promotionName") or "身份折扣"),
                    "realDiscount": enjoyed.get("realDiscount")}
    return {
        "identity_discount": identity,
        "price_without_card": yuan(p0),
        "price_with_card_ex_fee": yuan(p1 - fee),
        "card_fee": yuan(fee),
        "saving": yuan(p0 - (p1 - fee)),
        "raw": {"enjoyable": wd.get("enjoyable"), "enjoyed": enjoyed},
    }


def analyze(client: _BaseClient, min_orders: int = 5, margin: float = 1.2,
            max_orders: Optional[int] = None) -> Dict[str, Any]:
    warnings = []  # type: List[str]
    data = client.call_tool("order-list", {}).get("data") or {}
    orders = list(data.get("list") or [])
    if max_orders:
        orders = orders[:max_orders]

    rows = []  # type: List[Dict[str, Any]]
    cards = {}  # type: Dict[str, Dict[str, Any]]
    for o in orders:
        row = {"orderTime": o.get("createTime"), "store": o.get("storeName", ""),
               "items": _item_label(o), "orderType": o.get("orderType"),
               "beType": o.get("beType"), "_dt": _parse_time(o.get("createTime", ""))}
        reason = _skip_reason(o)
        if reason:
            row.update(status="skipped", reason=reason)
            rows.append(row)
            continue
        try:
            sc = o["storeCode"]
            if sc not in cards:
                cards[sc] = discover_card(client, o, warnings)
            res = replay_order(client, o, cards[sc])
            row.update(status="ok", **res)
        except AuthError:
            raise
        except McpError as e:
            row.update(status="skipped", reason="无法计算：%s" % e.message)
        except (KeyError, ValueError, TypeError):
            row.update(status="skipped", reason="无法计算：订单或响应字段缺失")
        rows.append(row)

    ok = [r for r in rows if r["status"] == "ok"]
    usable = len(ok)
    times = [r["_dt"] for r in ok if r["_dt"]]
    if len(times) >= 2:
        span = max(1, int(math.ceil((max(times) - min(times)).total_seconds() / 86400.0)))
    else:
        span = 1
    fees = [r["card_fee"] for r in ok]
    fee = statistics.median(fees) if ok else None
    gross = round(sum(r["saving"] for r in ok), 2)
    net = round(gross - fee, 2) if fee is not None else None
    proj = round(gross / span * 30, 2) if ok else 0.0
    avg = round(gross / usable, 2) if usable else None
    be_orders = round(fee / avg, 1) if (fee is not None and avg and avg > 0) else None
    low_conf = span < 7 or usable < min_orders

    identities = []  # type: List[Dict[str, Any]]
    for r in ok:
        idc = r.get("identity_discount")
        if idc and idc not in identities:
            identities.append(idc)
        r["note"] = ""
        if idc:
            r["note"] = "基准价已含身份折扣"
        elif r["saving"] < 0:
            r["note"] = "有卡价更高（卡优惠不适用于此商品）"
    has_identity = bool(identities)
    id_names = []  # type: List[str]
    for i in identities:
        if i["promotionName"] not in id_names:
            id_names.append(i["promotionName"])
    helped = sum(1 for r in ok if r["saving"] > 0)
    neutral = sum(1 for r in ok if r["saving"] == 0)
    worse = sum(1 for r in ok if r["saving"] < 0)

    if has_identity:
        d = identities[0]
        disc = "" if d["realDiscount"] in (None, "", 0) else " %s折" % d["realDiscount"]
        verdict = "无法判断（已有身份折扣）"
        why = ("你的账号已享受【%s%s】，它与随单购麦金卡优惠不叠加，试算时有卡价格反而更高，"
               "所以这次结果不代表普通用户的情况，也不建议据此购买；"
               "如需判断，请用没有身份折扣的账号或咨询门店" % (d["promotionName"], disc))
    elif usable == 0 or fee is None:
        verdict, why = "边缘/再观察", "没有可用于计算的订单，无法给出结论"
    elif proj >= fee * margin and usable >= min_orders:
        verdict, why = "值得买", "按近期频率折算，30 天预计节省 ¥%s，高于卡费 ¥%s 的 %.1f 倍门槛" % (fmt(proj), fmt(fee), margin)
    elif proj < fee:
        verdict, why = "不值得买", "按近期频率折算，30 天预计节省 ¥%s，低于卡费 ¥%s" % (fmt(proj), fmt(fee))
    else:
        verdict, why = "边缘/再观察", "预计节省与卡费接近或样本偏少（可用订单 %d，门槛 %d），再观察一段时间更稳妥" % (usable, min_orders)

    for r in rows:
        r.pop("_dt", None)
    skipped = [r for r in rows if r["status"] != "ok"]
    return {
        "verdict": verdict, "verdict_reason": why,
        "recommend_buy": verdict == "值得买",
        "baseline_has_identity_discount": has_identity,
        "identity_discounts": id_names,
        "card_fee_observed": {"values": sorted(set(fees)), "median": fee,
                              "min": min(fees) if fees else None,
                              "max": max(fees) if fees else None},
        "orders_helped": helped, "orders_neutral": neutral, "orders_worse": worse,
        "card_fee": fee, "usable_orders": usable, "total_orders": len(rows),
        "span_days": span, "gross_saving": gross, "net_saving_over_span": net,
        "projected_30d_gross_saving": proj, "avg_saving_per_order": avg,
        "breakeven_orders_per_30d": be_orders, "low_confidence": low_conf,
        "min_orders": min_orders, "margin": margin,
        "membership": {sc: {"membershipCode": c["membershipCode"], "specId": c["specId"],
                            "discovered": c["discovered"]} for sc, c in cards.items()},
        "orders": rows, "skipped_count": len(skipped), "warnings": warnings,
        "caveats": CAVEATS,
    }


CAVEATS = [
    "回放使用的是【今天】的菜单、价格和门店，不是你当时实际支付的价格。",
    "套餐按默认组成（productCode）回放，不还原你当时的自选替换。",
    "已下架/当前门店不可售的商品会被跳过并注明原因。",
    "订单列表仅返回最近约 10 单，样本小，结论请当作参考；样本不足会被标记。",
    "目前只回放到店点餐（含得来速）；麦乐送订单会被跳过。",
    "身份类折扣（如员工卡）与随单购麦金卡优惠不叠加：若检测到，本工具不给出买/不买结论。",
    "30 天节省额是按近期频率折算的【预测】，不是保证。",
    "本工具只读，不会下单或开通任何卡。",
]


# ---------------------------------------------------------------- 输出

def _cell(s: Any) -> str:
    return str(s).replace("|", "/").replace("\n", " ")


def render_markdown(res: Dict[str, Any], demo: bool) -> str:
    L = []  # type: List[str]
    if demo:
        L.append("> 【示例数据 / demo】以下全部为虚构数据，仅用于演示，不代表任何真实订单。")
        L.append("")
    L.append("# 麦回本：麦金卡值不值得买？")
    L.append("")
    L.append("| 时间 | 门店 | 商品 | 无卡价(¥) | 有卡价·不含卡费(¥) | 节省(¥) | 状态/备注 |")
    L.append("|---|---|---|---:|---:|---:|---|")
    for r in res["orders"]:
        if r["status"] == "ok":
            note = "已回放"
            if r.get("note"):
                note += "；" + r["note"]
            L.append("| %s | %s | %s | %s | %s | %s | %s |" % (
                _cell(r["orderTime"]), _cell(r["store"]), _cell(r["items"]),
                fmt(r["price_without_card"]), fmt(r["price_with_card_ex_fee"]), fmt(r["saving"]), _cell(note)))
        else:
            L.append("| %s | %s | %s | - | - | - | 跳过：%s |" % (
                _cell(r["orderTime"]), _cell(r["store"]), _cell(r["items"]), _cell(r["reason"])))
    L.append("")
    L.append("## 结论")
    L.append("")
    tail = ""
    if res["verdict"] == "不值得买":
        tail = "不划算就别买"
    elif res["verdict"] == "边缘/再观察":
        tail = "暂不建议购买"
    if tail:
        L.append("**%s**：%s；%s。" % (res["verdict"], res["verdict_reason"], tail))
    else:
        L.append("**%s**：%s。" % (res["verdict"], res["verdict_reason"]))
    L.append("")
    L.append("## 汇总")
    L.append("")
    L.append("- 可用订单：%d / %d（跳过 %d）" % (res["usable_orders"], res["total_orders"], res["skipped_count"]))
    L.append("- 统计跨度：%d 天" % res["span_days"])
    fo = res["card_fee_observed"]
    if fo["min"] is not None and fo["min"] != fo["max"]:
        L.append("- 月卡卡费（30 天）：¥%s（各订单观察到 ¥%s ~ ¥%s，取中位数；卡费以每次试算为准）" % (
            fmt(res["card_fee"]), fmt(fo["min"]), fmt(fo["max"])))
    else:
        L.append("- 月卡卡费（30 天）：¥%s（取自试算结果，以实际为准）" % fmt(res["card_fee"]))
    L.append("- 有卡更省 / 持平 / 更贵的订单：%d / %d / %d" % (
        res["orders_helped"], res["orders_neutral"], res["orders_worse"]))
    L.append("- 跨度内累计节省（未扣卡费）：¥%s" % fmt(res["gross_saving"]))
    L.append("- 跨度内净节省（累计节省 − 一张卡费）：¥%s" % fmt(res["net_saving_over_span"]))
    L.append("- 30 天预测节省（预测，非保证）：¥%s" % fmt(res["projected_30d_gross_saving"]))
    L.append("- 平均每单节省：¥%s" % fmt(res["avg_saving_per_order"]))
    if res["breakeven_orders_per_30d"] is not None:
        L.append("- 回本所需：约每 30 天 %s 单同类订单" % res["breakeven_orders_per_30d"])
    else:
        L.append("- 回本所需：按当前数据无法回本（平均每单节省不为正）")
    if res["low_confidence"]:
        L.append("- ⚠️ 低置信度：样本天数不足 7 天或可用订单少于 %d 单，请谨慎参考。" % res["min_orders"])
    if res["warnings"]:
        L.append("")
        L.append("## 提示")
        L.append("")
        for w in res["warnings"]:
            L.append("- " + w)
    L.append("")
    L.append("## 注意事项")
    L.append("")
    for c in res["caveats"]:
        L.append("- " + c)
    L.append("")
    L.append("_早餐卡相关为实验功能，见 `breakfast` 子命令，不提供购买建议。_")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- breakfast（实验）

def breakfast(client: _BaseClient) -> Dict[str, Any]:
    orders = (client.call_tool("order-list", {}).get("data") or {}).get("list") or []
    stores = {}  # type: Dict[str, Dict[str, Any]]
    for o in orders:
        if _skip_reason(o) is None and o.get("storeCode") not in stores:
            stores[o["storeCode"]] = o
    items = {}  # type: Dict[str, Dict[str, Any]]
    for o in stores.values():
        args = {"storeCode": o["storeCode"], "orderType": str(o["orderType"]), "beType": str(o["beType"])}
        if o.get("beCode"):
            args["beCode"] = o["beCode"]
        try:
            meals = (client.call_tool("query-meals", args).get("data") or {}).get("meals") or {}
        except McpError:
            continue
        for code, m in meals.items():
            if "早餐卡" in str(m.get("discountType", "")):
                items[code] = m
    hits = []
    notes = []  # type: List[str]
    for o in orders:
        if _skip_reason(o):
            continue
        for p in o.get("orderProductList", []):
            m = items.get(p.get("productCode"))
            if m:
                try:
                    gap = round(float(m["originalPrice"]) - float(m["currentPrice"]), 2)
                    qty = int(p.get("quantity", 1))
                except (KeyError, ValueError, TypeError):
                    notes.append("商品 %s 的价格或数量字段异常，已跳过。" % p.get("productCode"))
                    continue
                hits.append({"orderTime": o.get("createTime"), "name": m.get("name"),
                             "quantity": qty, "gap_per_item": gap,
                             "potential_gap": round(gap * qty, 2)})
    listed = []  # type: List[Dict[str, Any]]
    for c, m in items.items():
        try:
            listed.append({"productCode": c, "name": m.get("name"),
                           "currentPrice": float(m["currentPrice"]),
                           "originalPrice": float(m["originalPrice"]),
                           "gap": round(float(m["originalPrice"]) - float(m["currentPrice"]), 2)})
        except (KeyError, ValueError, TypeError):
            notes.append("菜单商品 %s 的价格字段异常，已跳过。" % c)
    return {"experimental": True, "breakfast_items": listed, "past_order_hits": hits,
            "notes": notes}


def render_breakfast(res: Dict[str, Any], demo: bool) -> str:
    L = []  # type: List[str]
    if demo:
        L.append("> 【示例数据 / demo】以下全部为虚构数据。")
        L.append("")
    L.append("# 早餐卡（实验功能，未验证）")
    L.append("")
    L.append("> 麦当劳 MCP 目前没有开放早餐卡的购买参数，所以这里**无法给出购买价格，也不建议/不提供购买建议**。"
             "下面仅列出菜单里标注「早餐卡优惠」的商品价差，属于【潜在、未验证】信息。")
    L.append("")
    if res["breakfast_items"]:
        L.append("| 商品 | 现价(¥) | 原价(¥) | 价差(¥，潜在、未验证) |")
        L.append("|---|---:|---:|---:|")
        for b in res["breakfast_items"]:
            L.append("| %s | %s | %s | %s |" % (_cell(b["name"]), fmt(float(b["currentPrice"])),
                                               fmt(float(b["originalPrice"])), fmt(b["gap"])))
    else:
        L.append("当前相关门店菜单中未发现「早餐卡优惠」商品。")
    L.append("")
    L.append("## 你过去订单中出现过的这类商品")
    L.append("")
    if res["past_order_hits"]:
        L.append("| 时间 | 商品 | 数量 | 价差合计(¥，潜在、未验证) |")
        L.append("|---|---|---:|---:|")
        for h in res["past_order_hits"]:
            L.append("| %s | %s | %d | %s |" % (_cell(h["orderTime"]), _cell(h["name"]),
                                                h["quantity"], fmt(h["potential_gap"])))
    else:
        L.append("没有找到。")
    L.append("")
    for n in res.get("notes", []):
        L.append("- 注：" + n)
    if res.get("notes"):
        L.append("")
    L.append("以上价差不含任何卡费，也不代表你实际能省多少。")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--demo", action="store_true", default=argparse.SUPPRESS,
                        help="离线演示（虚构的示例数据，无需 Token）")
    common.add_argument("--scenario", choices=["default", "identity"], default=argparse.SUPPRESS,
                        help="演示场景（配合 --demo）：identity = 基准价已含身份折扣")
    common.add_argument("--demo-identity", action="store_true", default=argparse.SUPPRESS,
                        help="等同于 --demo --scenario identity")
    p = argparse.ArgumentParser(prog="breakeven.py", parents=[common],
                                description="麦回本：用你自己的点餐记录算麦金卡是否回本（只读）")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("check", parents=[common], help="检查 Token 与连通性")
    a = sub.add_parser("analyze", parents=[common], help="回放近期订单，计算麦金卡是否回本")
    a.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    a.add_argument("--min-orders", type=int, default=5, help="判定“值得买”所需最少可用订单数（默认 5）")
    a.add_argument("--margin", type=float, default=1.2, help="30 天预测节省需达到卡费的倍数（默认 1.2）")
    a.add_argument("--max-orders", type=int, default=None, help="最多分析最近多少单")
    b = sub.add_parser("breakfast", parents=[common], help="（实验）早餐卡优惠价差，不提供购买建议")
    b.add_argument("--json", action="store_true", help="输出 JSON")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return EXIT_ERROR
    scenario = getattr(args, "scenario", "default")
    if getattr(args, "demo_identity", False):
        scenario = "identity"
    demo = getattr(args, "demo", False) or getattr(args, "demo_identity", False)

    if demo and args.cmd == "check":
        print("【示例数据 / demo】演示模式无需 Token，也不会联网。")
        return EXIT_OK

    client = DemoClient(scenario) if demo else McdClient(get_token())
    try:
        if args.cmd == "check":
            r = client.call_tool("now-time-info", {})
            print("连接正常，Token 有效。服务器时间信息：%s" % json.dumps(r.get("data"), ensure_ascii=False))
            return EXIT_OK
        if args.cmd == "analyze":
            res = analyze(client, args.min_orders, args.margin, args.max_orders)
            res["demo"] = demo
            if args.json:
                print(json.dumps(res, ensure_ascii=False, indent=2))
            else:
                sys.stdout.write(render_markdown(res, demo))
            return EXIT_OK
        if args.cmd == "breakfast":
            res = breakfast(client)
            res["demo"] = demo
            if args.json:
                print(json.dumps(res, ensure_ascii=False, indent=2))
            else:
                sys.stdout.write(render_breakfast(res, demo))
            return EXIT_OK
    except AuthError as e:
        sys.stderr.write("认证失败：%s\n请到 https://open.mcd.cn/mcp 重新获取并设置 MCD_MCP_TOKEN。\n" % e)
        return EXIT_AUTH
    except NetworkError as e:
        sys.stderr.write("%s\n请检查网络后重试。\n" % e)
        return EXIT_ERROR
    except McpError as e:
        sys.stderr.write("调用失败：%s\n" % e.message)
        return EXIT_ERROR
    except ForbiddenToolError as e:
        sys.stderr.write("%s\n" % e)
        return EXIT_ERROR
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
