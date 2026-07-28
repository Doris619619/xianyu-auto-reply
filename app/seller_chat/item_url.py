"""
本文件负责把用户随手粘贴的闲鱼商品链接规范化为可安全使用的详情页地址。

它属于 spike/seller_chat 实验模块。闲鱼分享出来的链接通常带 spm、categoryId 等追踪
参数，而聊天适配层会用 item_url_matches_binding 严格核对 URL，所以这里统一裁剪成
只保留 id 参数的官方 HTTPS 详情页。

本文件不访问网络、不解析页面、不打开浏览器，也不判断商品是否仍在售。
"""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from app.crawler.chat_client import item_url_matches_binding

# 闲鱼商品 ID 在配置和聊天绑定处都按纯数字处理，这里保持同一口径。
ITEM_ID_PATTERN = re.compile(r"^\d{1,32}$")
ACCEPTED_HOSTS = frozenset({"goofish.com", "www.goofish.com", "m.goofish.com"})
CANONICAL_ITEM_URL_TEMPLATE = "https://www.goofish.com/item?id={item_id}"


class ItemUrlError(ValueError):
    """
    表示输入的商品链接无法安全解析为闲鱼商品。

    异常文本只包含输入格式问题的说明，不回显可能含追踪参数的完整原始链接。
    """


@dataclass(frozen=True, slots=True)
class ItemReference:
    """
    保存一次实验绑定的商品标识与规范化详情页地址。

    ``detail_url`` 保证能通过 ``item_url_matches_binding`` 校验，可直接交给聊天工厂。
    """

    item_id: str
    detail_url: str


def parse_item_reference(raw: str) -> ItemReference:
    """
    从商品链接或纯商品 ID 解析出规范化的商品引用。

    参数 ``raw`` 可以是完整闲鱼详情页链接（允许携带 spm、categoryId 等任意追踪参数），
    也可以是纯数字商品 ID。返回同时包含 item_id 和规范化 HTTPS 详情页 URL 的引用。

    输入为空、主机非官方、路径不是 /item、缺少或存在多个 id 参数、id 不是数字时抛出
    ``ItemUrlError``。函数只做字符串解析，没有任何网络或文件副作用。
    """

    candidate = raw.strip()
    if not candidate:
        raise ItemUrlError("商品链接不能为空")

    item_id = candidate if ITEM_ID_PATTERN.fullmatch(candidate) else _extract_item_id(candidate)
    detail_url = CANONICAL_ITEM_URL_TEMPLATE.format(item_id=item_id)
    if not item_url_matches_binding(detail_url, item_id):
        # 规范化结果必须能通过聊天适配层的同一套校验，否则说明模板或校验规则已经漂移。
        raise ItemUrlError("规范化后的详情页地址未通过闲鱼商品 URL 校验")
    return ItemReference(item_id=item_id, detail_url=detail_url)


def _extract_item_id(candidate: str) -> str:
    """
    从完整闲鱼详情页链接中提取唯一数字商品 ID。

    参数为已去除首尾空白的非空字符串；返回纯数字商品 ID。协议、主机、路径或 id 参数
    任一不满足官方详情页形态时抛出 ``ItemUrlError``。函数无外部副作用。
    """

    parsed = urlparse(candidate if "://" in candidate else f"https://{candidate}")
    if parsed.scheme not in {"http", "https"}:
        raise ItemUrlError("商品链接必须是 HTTP 或 HTTPS 地址")
    if (parsed.hostname or "").casefold() not in ACCEPTED_HOSTS:
        raise ItemUrlError("只接受 goofish.com 官方域名下的闲鱼商品链接")
    if parsed.path.rstrip("/") != "/item":
        raise ItemUrlError("商品链接路径必须是 /item，请使用商品详情页地址")

    item_ids = parse_qs(parsed.query, keep_blank_values=True).get("id", [])
    if len(item_ids) != 1:
        raise ItemUrlError("商品链接必须且只能包含一个 id 参数")
    item_id = item_ids[0].strip()
    if not ITEM_ID_PATTERN.fullmatch(item_id):
        raise ItemUrlError("商品链接中的 id 参数必须是纯数字")
    return item_id
