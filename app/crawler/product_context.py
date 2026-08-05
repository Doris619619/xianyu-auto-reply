"""
本文件从闲鱼商品详情页提取用于议价的最小商品上下文。

它优先读取结构化商品价格和详情头部的价格节点；推荐商品、浮层等其它金额不会覆盖主商品
价格。提取不到可靠主价时明确返回未知，不猜测金额。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from playwright.async_api import Page

_MONEY_PATTERN = re.compile(r"[¥￥]\s*(\d{1,9}(?:\.\d{1,2})?)")
_FREIGHT_PATTERN = re.compile(r"(?:含运费|运费)\s*(\d{1,9}(?:\.\d{1,2})?)\s*元?")


@dataclass(frozen=True, slots=True)
class ProductContext:
    """表示从当前商品页可靠读取到的标题、标价和运费。"""

    title: str | None = None
    list_price: Decimal | None = None
    freight: Decimal | None = None
    source: str = "unknown"

    @property
    def list_price_yuan_floor(self) -> int | None:
        """返回不超过标价的整数元，用于受控自动报价上限。"""

        return int(self.list_price) if self.list_price is not None else None


async def extract_product_context(page: Page) -> ProductContext:
    """只读提取商品主价；主价歧义或页面异常时保守返回未知。"""

    raw = await page.evaluate(
        r"""() => {
          const visible = (node) => {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden'
              && Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
          };
          const candidates = [];
          const seen = new Set();
          const push = (node, semantic) => {
            if (!node || seen.has(node) || !visible(node)) return;
            seen.add(node);
            const rect = node.getBoundingClientRect();
            const className = typeof node.className === 'string' ? node.className : '';
            const attributes = [
              node.getAttribute('data-price'), node.getAttribute('data-testid'),
              node.getAttribute('aria-label'), node.getAttribute('content')
            ].filter(Boolean).join(' ');
            const text = `${attributes} ${node.textContent || ''}`.trim();
            if (!/[¥￥]\s*\d/.test(text)) return;
            const fontSize = Number.parseFloat(window.getComputedStyle(node).fontSize) || 0;
            const score = (semantic ? 100 : 0)
              + (rect.top >= 0 && rect.top < 700 ? 30 : 0)
              + (fontSize >= 20 ? 10 : 0)
              + (node.hasAttribute('data-price') ? 40 : 0);
            candidates.push({ text, score, top: Math.max(0, Math.round(rect.top)), semantic });
          };
          const semanticSelector = [
            '[data-price]', '[itemprop="price"]', 'meta[property="product:price:amount"]',
            'meta[itemprop="price"]', '[data-testid*="price" i]', '[class*="price" i]',
            '[class*="amount" i]'
          ].join(',');
          document.querySelectorAll(semanticSelector).forEach((node) => push(node, true));
          Array.from(document.body?.querySelectorAll('*') || []).slice(0, 5000).forEach((node) => {
            const text = (node.textContent || '').trim();
            if (text.length <= 48 && /^[¥￥]\s*\d/.test(text)) push(node, false);
          });
          return {
            jsonLd: Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
              .map((node) => node.textContent || ''),
            metaPrices: Array.from(document.querySelectorAll(
              'meta[property="product:price:amount"], meta[itemprop="price"]'
            )).map((node) => node.getAttribute('content') || ''),
            title: document.querySelector('meta[property="og:title"]')?.getAttribute('content')
              || document.querySelector('h1')?.textContent || document.title || '',
            priceCandidates: candidates,
            bodyText: document.body?.innerText || ''
          };
        }"""
    )
    if not isinstance(raw, dict):
        return ProductContext()
    title = _clean_text(raw.get("title"))
    structured = _structured_context(raw.get("jsonLd"), title)
    if structured.list_price is not None:
        return structured
    meta_price = _unique_price(raw.get("metaPrices"))
    if meta_price is not None:
        return ProductContext(title=title, list_price=meta_price, source="metadata")
    dom_context = _dom_context(title, raw.get("priceCandidates"))
    if dom_context.list_price is not None:
        return dom_context
    return _visible_context(title, raw.get("bodyText"))


def _structured_context(raw_scripts: object, fallback_title: str | None) -> ProductContext:
    """从 JSON-LD 的 Product/Offer 数据读取唯一标价和运费。"""

    if not isinstance(raw_scripts, list):
        return ProductContext(title=fallback_title)
    for raw in raw_scripts:
        if not isinstance(raw, str):
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        for node in _walk_json(data):
            offers = node.get("offers") if isinstance(node, dict) else None
            offer = offers[0] if isinstance(offers, list) and offers else offers
            if not isinstance(offer, dict):
                continue
            price = _to_money(offer.get("price"))
            if price is None:
                continue
            freight = _to_money(offer.get("shippingRate"))
            return ProductContext(
                title=_clean_text(node.get("name")) or fallback_title,
                list_price=price,
                freight=freight,
                source="structured",
            )
    return ProductContext(title=fallback_title)


def _dom_context(title: str | None, raw_candidates: object) -> ProductContext:
    """从详情头部的可见主价候选中选择唯一最高优先级金额。"""

    if not isinstance(raw_candidates, list):
        return ProductContext(title=title)
    scored: dict[Decimal, int] = {}
    source_by_price: dict[Decimal, str] = {}
    for candidate in raw_candidates:
        if not isinstance(candidate, dict):
            continue
        raw_text = str(candidate.get("text", ""))
        values = {_to_money(value) for value in _MONEY_PATTERN.findall(raw_text)}
        values.discard(None)
        if len(values) != 1:
            continue
        price = next(iter(values))
        assert price is not None
        score = candidate.get("score")
        if not isinstance(score, int):
            continue
        if score > scored.get(price, -1):
            scored[price] = score
            source_by_price[price] = "dom_main" if candidate.get("semantic") else "dom_viewport"
    if not scored:
        return ProductContext(title=title)
    best_score = max(scored.values())
    winners = [price for price, score in scored.items() if score == best_score]
    if len(winners) != 1:
        return ProductContext(title=title)
    price = winners[0]
    return ProductContext(title=title, list_price=price, source=source_by_price[price])


def _visible_context(title: str | None, raw_body: object) -> ProductContext:
    """仅在整页文本只有一个货币金额时作为最后的保守兜底。"""

    if not isinstance(raw_body, str):
        return ProductContext(title=title)
    price = _unique_price(_MONEY_PATTERN.findall(raw_body[:12_000]))
    freight_match = _FREIGHT_PATTERN.search(raw_body[:12_000])
    freight = _to_money(freight_match.group(1)) if freight_match else None
    return ProductContext(title=title, list_price=price, freight=freight, source="visible")


def _unique_price(values: object) -> Decimal | None:
    """将一组候选金额收敛为唯一合法金额；多值或缺失均返回未知。"""

    if not isinstance(values, list):
        return None
    prices = {_to_money(value) for value in values}
    prices.discard(None)
    return next(iter(prices)) if len(prices) == 1 else None


def _walk_json(value: object):
    """深度遍历 JSON 容器中的字典，不访问外部状态。"""

    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _to_money(value: object) -> Decimal | None:
    """将非负有限金额规范为两位以内小数，非法值返回 None。"""

    if isinstance(value, bool) or value is None:
        return None
    try:
        money = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if not money.is_finite() or money < 0 or money.as_tuple().exponent < -2:
        return None
    return money


def _clean_text(value: object) -> str | None:
    """将页面文本压缩为短标题，空值返回 None。"""

    if not isinstance(value, str):
        return None
    text = " ".join(value.split())[:512]
    return text or None
