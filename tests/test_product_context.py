"""
本文件验证闲鱼商品详情页上下文的保守提取。

它只使用内存页面返回值，覆盖结构化标价、可见价格与歧义价格，不访问浏览器或网络。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.crawler.product_context import ProductContext, extract_product_context
from app.seller_chat.item_url import ItemReference
from app.seller_chat.prompts import build_opening_brief


class FakeProductPage:
    """返回预设详情页快照的最小 Page 替身。"""

    def __init__(self, value: object) -> None:
        self.value = value

    async def evaluate(self, script: str) -> object:
        assert "application/ld+json" in script
        return self.value


@pytest.mark.asyncio
async def test_extracts_structured_title_price_and_freight() -> None:
    """优先从 JSON-LD Product/Offer 取得详情页可信字段。"""

    context = await extract_product_context(
        FakeProductPage(
            {
                "title": "页面标题",
                "bodyText": "¥9999",
                "jsonLd": [
                    '{"@type":"Product","name":"显卡","offers":'
                    '{"price":"69888.00","shippingRate":"0.00"}}'
                ],
            }
        )  # type: ignore[arg-type]
    )

    assert context.title == "显卡"
    assert context.list_price == Decimal("69888.00")
    assert context.freight == Decimal("0.00")
    assert context.source == "structured"


@pytest.mark.asyncio
async def test_visible_single_currency_price_is_conservative_fallback() -> None:
    """没有结构化数据时，只接受唯一的可见货币金额。"""

    context = await extract_product_context(
        FakeProductPage(
            {"title": "闲置相机", "bodyText": "商品标价 ¥1200\n含运费 15 元", "jsonLd": []}
        )  # type: ignore[arg-type]
    )

    assert context.title == "闲置相机"
    assert context.list_price == Decimal("1200")
    assert context.freight == Decimal("15")
    assert context.source == "visible"


@pytest.mark.asyncio
async def test_main_price_dom_wins_over_recommendation_prices() -> None:
    """详情主价节点应优先于同页推荐商品的其它金额。"""

    context = await extract_product_context(
        FakeProductPage(
            {
                "title": "闲置配件",
                "bodyText": "¥86\n猜你喜欢 ¥199",
                "jsonLd": [],
                "metaPrices": [],
                "priceCandidates": [
                    {"text": "¥86.00", "score": 140, "semantic": True},
                    {"text": "¥199", "score": 30, "semantic": False},
                ],
            }
        )  # type: ignore[arg-type]
    )

    assert context.list_price == Decimal("86.00")
    assert context.source == "dom_main"


@pytest.mark.asyncio
async def test_ambiguous_main_price_dom_remains_unknown() -> None:
    """两个同优先级主价候选不能擅自挑一个金额。"""

    context = await extract_product_context(
        FakeProductPage(
            {
                "title": "闲置",
                "bodyText": "¥86 ¥99",
                "jsonLd": [],
                "metaPrices": [],
                "priceCandidates": [
                    {"text": "¥86", "score": 140, "semantic": True},
                    {"text": "¥99", "score": 140, "semantic": True},
                ],
            }
        )  # type: ignore[arg-type]
    )

    assert context.list_price is None


@pytest.mark.asyncio
async def test_multiple_visible_prices_leave_listing_price_unknown() -> None:
    """可见文本出现多个候选金额时绝不猜测其中任意一个。"""

    context = await extract_product_context(
        FakeProductPage(
            {"title": "闲置", "bodyText": "现价 ¥1200\n参考价 ¥1500", "jsonLd": []}
        )  # type: ignore[arg-type]
    )

    assert context.list_price is None
    assert context.source == "visible"


def test_opening_brief_carries_reliable_price_and_unknown_price_rule() -> None:
    """开场与裁决共用的商品背景必须明确告诉模型价格是否可靠。"""

    item = ItemReference(item_id="123", detail_url="https://www.goofish.com/item?id=123")
    known = build_opening_brief(
        item,
        None,
        product=ProductContext(
            title="显卡", list_price=Decimal("69888"), freight=Decimal("0")
        ),
    )
    unknown = build_opening_brief(item, None)

    assert "商品标价：69888 元" in known
    assert "运费：0 元" in known
    assert "未能可靠读取，禁止主动报具体金额" in unknown
