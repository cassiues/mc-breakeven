---
name: mc-breakeven
description: 麦回本 - 用你自己的麦当劳点餐记录，算清楚【麦金卡】(随单购月卡) 值不值得买、多久回本。只读，不下单。Triggers 触发词：麦金卡 值不值得买 回本 随单购 麦当劳 省钱 麦当劳MCP；English - McDonald's China MCP, McGold card, break-even, is the monthly card worth it, savings calculator.
---

# 麦回本 (mc-breakeven)

回答一个问题：**按我自己的点餐习惯，这张【麦金卡】(随单购月卡) 值不值得买？**

做法：读取用户最近的订单，用官方价格计算器分别算“不带卡”和“带卡”的价格，扣掉卡费后汇总净节省，给出 值得买 / 不值得买 / 边缘 / 无法判断（已有身份折扣） 的结论。基调是：算清楚再决定，不划算就别买。

## 只读保证

- 只调用白名单内的 MCP 工具：order-list、query-meals、query-meal-detail、calculate-price、query-nearby-stores、query-my-account、now-time-info。
- 绝不调用 create-order、cancel-order、mall-create-order、draw-lottery、party-order-create、auto-bind-coupons、delivery-create-address 等任何会产生副作用的工具（代码里会直接拒绝）。
- 任何会产生实际动作的事情（下单、开卡、领券等），**必须先向用户确认**；本技能自身不提供这些动作。

## 使用步骤

1. 检查环境变量 `MCD_MCP_TOKEN` 是否已设置（不要读取或回显它的值）。
2. 如果没有设置：告诉用户获取方式，然后**停止并请用户自己设置后再继续**：
   - 打开 https://open.mcd.cn/mcp -> 登录 -> 控制台 -> 激活，获得 MCP Token；
   - 在终端设置：macOS/Linux `export MCD_MCP_TOKEN="..."`；Windows PowerShell `$env:MCD_MCP_TOKEN="..."`；
   - 不要让用户把 Token 贴进聊天、仓库或任何文件里。
   - 想先体验可以用 `--demo`（离线虚构数据，无需 Token）。
3. 连通性检查：`python3 scripts/breakeven.py check`
4. 分析：`python3 scripts/breakeven.py analyze`（可选 `--json`、`--min-orders 5`、`--margin 1.2`、`--max-orders N`）
5. 如实转述结果：表格、结论、汇总和注意事项都要给用户看。
   - 结论是“值得买”才可以说值得；
   - 结论是“不值得买”或“边缘/再观察”时，**明确告诉用户：不建议购买**；
   - 结论是“无法判断（已有身份折扣）”时：用平实的话告诉用户——账号已享受身份折扣（如员工卡），它与随单购麦金卡优惠不叠加，所以试算里带卡反而更贵；这个结果不代表普通用户，**不要劝购、不要暗示可以买**，建议用没有身份折扣的账号或咨询门店；
   - 任何情况下都不要推动用户购买；
   - 强调 30 天节省额是按近期频率折算的预测，不是保证。
6. 早餐卡：`python3 scripts/breakeven.py breakfast` 仅为**实验功能**，只展示价差（潜在、未验证）；MCP 暂未开放早餐卡购买参数，不要给出购买价格或购买建议。

演示：`python3 scripts/breakeven.py --demo analyze`（输出会标注【示例数据 / demo】）。

## 退出码

0 成功；1 一般错误/网络错误；2 未设置 `MCD_MCP_TOKEN`；3 Token 无效（401）。

## 呈现要点与限制

- 回放用的是今天的菜单与价格，不是当时实际支付价；套餐按默认组成回放；下架商品会被跳过并注明原因。
- 订单列表只有最近约 10 单，样本小要提示；身份折扣与麦金卡优惠不叠加，检测到时只给“无法判断”，不下买/不买结论。卡费以每次试算为准，汇总里要如实说明所用卡费（及范围）。
- 目前只回放到店点餐（含得来速），麦乐送订单会被跳过。
- 只谈麦当劳内部“有卡/无卡”的对比，不与其他品牌比较，不鼓励多买、多吃。
- 非官方项目，与麦当劳无关联；仅限个人非商业使用。
