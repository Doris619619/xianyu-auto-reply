"""
本文件集中保存经生产登录态标定确认的闲鱼聊天选择器。

它属于 crawler 页面适配模块，只描述商品聊天入口、聊天主体、输入、发送和消息节点。
选择器优先使用 URL 参数、元素语义和稳定类名前缀，不保存哈希后缀、账号、卖家、聊天正文
或 Cookie，也不包含购买、付款、地址和确认订单控件。页面结构漂移时客户端必须失败关闭。

2026-07-24 初版标定使用 ``msg-text-(left|right)--`` 且要求为 ``message-content`` 直接子节点；
2026-07-26 真机确认气泡类名已变为 ``message-text-(left|right)--``，并嵌套在 content 内部，
因此选择器同时兼容新旧前缀，并改为后代匹配。
"""

BODY_SELECTOR = "body"

# 闲鱼会调整动态 CSS 类；身份参数与用户可见动作语义共同构成稳定边界。只接受同时
# 携带商品/对端用户参数且文案为主商品动作“聊一聊”或“我想要”的链接。侧栏“消息”
# 即使携带相同参数也必须排除，调用方还会继续核对唯一性、商品、卖家和买家账号。
BOUND_CHAT_LINK_SELECTOR = "a[href*='/im?'][href*='itemId='][href*='peerUserId=']"
OPEN_CHAT_SELECTOR = ", ".join(
    (
        f"{BOUND_CHAT_LINK_SELECTOR}:has-text('聊一聊')",
        f"{BOUND_CHAT_LINK_SELECTOR}:has-text('我想要')",
    )
)
CHAT_ENTRY_WAIT_MILLISECONDS = 8_000
CHAT_READY_WAIT_MILLISECONDS = 8_000
CHAT_PANEL_SELECTOR = "main[class*='chat-main--']"
CHAT_MESSAGE_LIST_SELECTOR = "div[class*='message-list-reverse--']"
CHAT_INPUT_SELECTOR = "textarea[placeholder='请输入消息，按Enter键发送或点击发送按钮发送']"
CHAT_SEND_SELECTOR = "div[class*='sendbox-bottom--'] > button"

# 每条消息内容节点包含 left/right 方向类；系统商品卡也会成为基线，但不会被当成回复文本发送。
# 使用后代选择器：真实页气泡在 message-content 内还有一层包装，不是直接子节点。
# 每个候选必须单独写成完整选择器，避免逗号把「列表/content 前缀」作用域拆掉。
CHAT_MESSAGE_SELECTOR = ", ".join(
    (
        f"{CHAT_MESSAGE_LIST_SELECTOR} div[class*='message-content--'] "
        "div[class*='msg-text-left--']",
        f"{CHAT_MESSAGE_LIST_SELECTOR} div[class*='message-content--'] "
        "div[class*='message-text-left--']",
        f"{CHAT_MESSAGE_LIST_SELECTOR} div[class*='message-content--'] "
        "div[class*='msg-text-right--']",
        f"{CHAT_MESSAGE_LIST_SELECTOR} div[class*='message-content--'] "
        "div[class*='message-text-right--']",
    )
)
OWN_CHAT_MESSAGE_SELECTOR = ", ".join(
    (
        f"{CHAT_MESSAGE_LIST_SELECTOR} div[class*='message-content--'] "
        "div[class*='msg-text-right--']",
        f"{CHAT_MESSAGE_LIST_SELECTOR} div[class*='message-content--'] "
        "div[class*='message-text-right--']",
    )
)

MESSAGE_ID_ATTRIBUTES = ("data-message-id", "data-id")
MESSAGE_DIRECTION_ATTRIBUTES = ("class",)
MESSAGE_TIMESTAMP_ATTRIBUTES = ("data-timestamp", "data-created-at", "datetime")

# 当前登录买家只通过服务端 Cookie 的指纹绑定；原值不得进入日志、事件或数据库。
ACCOUNT_IDENTITY_COOKIE_NAME = "tracknick"
