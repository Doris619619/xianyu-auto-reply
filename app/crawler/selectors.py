"""
本文件集中存放闲鱼页面登录与风控相关的文本信号常量。

它属于 crawler 模块，只提供只读常量，不访问网络或页面。
"""

LOGIN_URL_FRAGMENTS = ("passport.goofish.com", "mini_login", "/login")
RISK_TEXT_SIGNALS = (
    "验证码",
    "安全验证",
    "访问频繁",
    "账号异常",
    "操作受限",
    "操作频繁",
    "请登录",
    "非法访问",
    "请使用正常浏览器",
)
