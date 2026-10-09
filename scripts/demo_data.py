# -*- coding: utf-8 -*-
"""【示例数据 / demo】全部为虚构数据，仅用于 --demo 离线演示。

这里没有任何真实订单号、门店、账号或积分信息。
`handle(name, args)` 模拟 MCP 工具返回的 JSON 对象（已解析，金额单位：分）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

DEMO_LABEL = "【示例数据 / demo】"

DEMO_STORE_CODE = "DEMO0001"
DEMO_STORE_NAME = "示例餐厅·演示店"
CARD_PRODUCT_CODE = "DEMOCARD"
CARD_NAME = "麦金卡月卡（30天）"
CARD_FEE_CENTS = 1900
IDENTITY_FEE_CENTS = 1445
SCENARIO = "default"  # 由 DemoClient 设置；"identity" = 基准价已含身份折扣（员工卡 7.5 折）

# productCode -> (名称, 无卡价/分, 有卡价/分)
CATALOG = {
    "D1001": ("示例牛肉堡", 2200, 2000),
    "D1002": ("示例鸡肉堡", 1800, 1700),
    "D1003": ("示例薯条(中)", 1000, 1000),
    "D1004": ("示例可乐(中)", 900, 800),
    "D1005": ("示例鸡块", 1500, 1400),
    "D1006": ("示例甜筒", 600, 600),
    "B2001": ("示例早餐堡", 1200, 1200),
}

# 早餐卡优惠（仅用于 breakfast 实验功能演示）: productCode -> (名称, 原价, 现价)
BREAKFAST = {
    "B2001": ("示例早餐堡", 1200, 900),
    "B2002": ("示例豆浆", 800, 600),
}

# (几天前, 小时, 订单类型, beType, [(productCode, 数量), ...], 状态)
_ORDER_SPECS = [
    (1, 12, "1", "1", [("D1001", 1), ("D1004", 1)], "已完成"),
    (2, 19, "1", "1", [("D1002", 2), ("D1003", 1)], "已完成"),
    (3, 8, "1", "1", [("B2001", 1), ("D1004", 1)], "已完成"),
    (4, 12, "1", "1", [("D1005", 1), ("D1004", 1)], "已完成"),
    (5, 18, "2", "2", [("D1001", 1), ("D1003", 1)], "已完成"),
    (7, 13, "1", "1", [("D1001", 1), ("D1003", 1), ("D1004", 1)], "已完成"),
    (9, 12, "1", "1", [("D1002", 1), ("D1004", 1)], "已完成"),
    (10, 20, "1", "1", [("D1006", 2)], "已完成"),
    (12, 12, "1", "1", [("D1001", 2), ("D1004", 2)], "已完成"),
    (14, 19, "1", "1", [("D1005", 1), ("D1003", 1)], "已取消"),
    (15, 12, "1", "5", [("D1002", 1), ("D1005", 1)], "已完成"),
    (17, 13, "1", "1", [("D1001", 1), ("D1004", 1)], "已完成"),
    (19, 12, "1", "1", [("D1002", 1), ("D1099", 1)], "已完成"),
    (21, 19, "1", "1", [("D1001", 1), ("D1005", 1), ("D1004", 1)], "已完成"),
    (24, 12, "1", "1", [("D1002", 1), ("D1004", 1)], "已完成"),
]


def _orders():
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    out = []
    for i, (days, hour, otype, betype, items, status) in enumerate(_ORDER_SPECS):
        t = (now - timedelta(days=days)).replace(hour=hour)
        plist = []
        total = 0
        for code, qty in items:
            name, price, _ = CATALOG.get(code, ("示例下架商品", 1000, 1000))
            total += price * qty
            plist.append({"productCode": code, "productName": name,
                          "quantity": qty, "comboItemList": []})
        order = {
            "orderId": "DEMO-ORDER-%03d" % (i + 1),
            "orderType": otype,
            "createTime": t.strftime("%Y-%m-%d %H:%M:%S"),
            "beType": betype,
            "storeCode": DEMO_STORE_CODE,
            "storeName": DEMO_STORE_NAME,
            "orderStatus": status,
            "realTotalAmount": "%.2f" % (total / 100.0),
            "orderProductList": plist,
        }
        if betype == "5":
            order["beCode"] = "DEMO-BE"
        out.append(order)
    return out


def _meals():
    meals = {}
    for code, (name, price, card) in CATALOG.items():
        if card < price:
            meals[code] = {
                "name": name, "currentPrice": card / 100.0,
                "originalPrice": price / 100.0,
                "discountType": "随单购麦金卡优惠", "canWithOrder": True,
                "withOrder": {"membershipCode": "DEMO-M", "specId": "DEMO-S"},
            }
        else:
            meals[code] = {"name": name, "currentPrice": price / 100.0,
                           "originalPrice": price / 100.0,
                           "discountType": "", "canWithOrder": False}
    for code, (name, orig, cur) in BREAKFAST.items():
        meals[code] = {"name": name, "currentPrice": cur / 100.0,
                       "originalPrice": orig / 100.0,
                       "discountType": "早餐卡优惠", "canWithOrder": False}
    return meals


def price(items, with_card):
    """模拟 calculate-price：返回 (总价/分, productList)；未知商品抛 KeyError。"""
    lines, total = [], 0
    ident = SCENARIO == "identity"
    fee = IDENTITY_FEE_CENTS if ident else CARD_FEE_CENTS
    for it in items:
        name, p, c = CATALOG[it["productCode"]]
        # identity 场景：无卡时享受 7.5 折；有卡时身份折扣被卡优惠替换（不叠加）
        unit = c if with_card else (int(round(p * 0.75)) if ident else p)
        sub = unit * int(it["quantity"])
        total += sub
        lines.append({"productCode": it["productCode"], "productName": name,
                      "quantity": int(it["quantity"]),
                      "originalSubtotal": p * int(it["quantity"]), "subtotal": sub})
    if with_card:
        total += fee
        lines.append({"productCode": CARD_PRODUCT_CODE, "productName": CARD_NAME,
                      "quantity": 1, "originalSubtotal": fee,
                      "subtotal": fee})
    return total, lines


class DemoError(Exception):
    """模拟 success:false 的业务错误。"""

    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


def handle(name, args):
    if name == "order-list":
        return {"success": True, "code": 200, "message": "demo",
                "data": {"list": _orders()}}
    if name == "query-meals":
        return {"success": True, "code": 200, "message": "demo",
                "data": {"meals": _meals()}}
    if name == "calculate-price":
        try:
            total, lines = price(args["items"], bool(args.get("withOrder")))
        except KeyError:
            raise DemoError("DEMO_NOT_SOLD", "商品已下架或当前门店不可售")
        data = {"price": total, "productList": lines}
        if not args.get("withOrder") and SCENARIO == "identity":
            data["enjoyed"] = {"promotionName": "示例员工卡", "realDiscount": 7.5, "amountType": "2"}
        elif not args.get("withOrder"):
            data["enjoyable"] = {"amountType": "demo", "realDiscount": 0, "balance": 0}
        return {"success": True, "code": 200, "message": "demo", "data": data}
    if name == "now-time-info":
        return {"success": True, "code": 200, "message": "demo",
                "data": {"now": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}}
    raise DemoError("DEMO_UNSUPPORTED", "演示模式不支持工具: %s" % name)
