"""
本文件内联议价工具使用的确定性内容黑名单正则。

它属于 services 模块，供 seller_chat.guardrails 导入。
刻意不导出议价词黑名单（NEGOTIATION），因为本产品的目标就是议价。
不访问网络、不发送消息。
"""

import re

EXTERNAL_LINK_PATTERN = re.compile(
    r"(?:https?://|www\.|[\w-]+\.(?:com|cn|net|jp)(?:/|\b))",
    re.IGNORECASE,
)
PHONE_OR_EMAIL_PATTERN = re.compile(
    r"(?:\b1[3-9]\d{9}\b|\b0\d{1,4}-?\d{6,9}\b|\b\d{3}-\d{4}-\d{4}\b|"
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})",
    re.IGNORECASE,
)
OFF_PLATFORM_PATTERN = re.compile(
    r"(?:微信|微\s*信|vx|v信|QQ|Line|ライン|加我|私聊)",
    re.IGNORECASE,
)
PAYMENT_PATTERN = re.compile(
    r"(?:支付宝|银行卡|转账|汇款|二维码|付款链接|定金|私下交易)",
    re.IGNORECASE,
)
ADDRESS_PATTERN = re.compile(
    r"(?:收货地址|详细地址|邮编|邮政编码|住所|送付先|電話番号)",
    re.IGNORECASE,
)
PURCHASE_COMMITMENT_PATTERN = re.compile(
    r"(?:(?:我|我们).{0,5}(?:要了|买了|购买|拍下|下单|付款)|"
    r"确认购买|马上付款|现在付款|接受加价|确认收货|给我发货)",
    re.IGNORECASE,
)
CREDENTIAL_PATTERN = re.compile(
    r"(?:验证码|短信码|登录码|安全验证|人机验证|captcha|verification\s*code)",
    re.IGNORECASE,
)
PROMPT_INJECTION_PATTERN = re.compile(
    r"(?:忽略.{0,12}(?:指令|规则|系统)|system\s+prompt|developer\s+message|"
    r"ignore.{0,20}(?:instruction|previous)|你现在是.{0,20}(?:助手|系统))",
    re.IGNORECASE,
)
SELLER_PURCHASE_ESCALATION_PATTERN = re.compile(
    r"(?:现在拍吗|要不要拍|可以拍下|直接拍|什么时候付款|等你付款|给你改价)",
    re.IGNORECASE,
)
