"""
本文件从本地登录态打印 tracknick 的 SHA-256，供配置账号指纹。

它属于 scripts，按当前浏览器后端解析登录态路径后只读文件并输出指纹；
不启动浏览器、不访问网络。
"""

from __future__ import annotations

import hashlib
import json
import sys

from app.core.config import get_settings


def main() -> int:
    """
    读取登录态中的 tracknick Cookie，打印其 SHA-256。

    成功返回 0；文件缺失或找不到 Cookie 时返回非 0。
    """

    settings = get_settings()
    path = settings.resolved_storage_state_path()
    if not path.is_file():
        print(
            f"登录态不存在：{path}。"
            f"请先在 XIANYU_BROWSER_BACKEND={settings.xianyu_browser_backend} "
            "下运行 scripts/login_xianyu.py",
            file=sys.stderr,
        )
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))
    cookies = data.get("cookies") or []
    values = [
        c.get("value", "")
        for c in cookies
        if isinstance(c, dict) and c.get("name") == "tracknick" and c.get("value")
    ]
    if not values:
        print("未找到 tracknick Cookie，请重新登录后再试。", file=sys.stderr)
        return 2
    digest = hashlib.sha256(values[0].encode("utf-8")).hexdigest()
    print(digest)
    print("请将上面的值写入 .env 的 XIANYU_EXPECTED_ACCOUNT_ID", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
