# 已知限制与真机验收

## 已知限制

- 本工具会真实向卖家发送消息；请先用无所谓的测试商品联调  
- 长时间挂着浏览器轮询聊天页有风控风险；退避会逐渐变疏  
- 页面 DOM 变更可能导致打开聊天或发送失败，需重新标定选择器  
- 同意/拒绝降价使用确定性正则，无法覆盖所有口语表达  
- 进程退出后 Playwright 会话结束；队列状态在 SQLite 中保留，但聊天上下文需从页面重读  
- 缺 `DEEPSEEK_API_KEY` 或登录态时，面板仍可入队，但 Worker 启动会失败  
- MVP 面板为静态 HTML 短轮询，未使用 WebSocket  

## 不做的事

- 自动购买、付款、填地址、确认收货  
- Chrome 扩展  
- 商城订单绑定 / 采购白名单  
- 扫描无关私聊  

## 真机验收清单

1. `python -m pytest tests -q` 全绿  
2. `python scripts/login_xianyu.py` 生成 `storage_state.json`  
3. 运行 `python scripts/print_account_fingerprint.py`，把输出写入 `XIANYU_EXPECTED_ACCOUNT_ID`  
4. 配置 `DEEPSEEK_API_KEY` 后 `python -m app.main`  
5. 面板贴 3 个测试链接，启动 Worker：只开第 1 家  
6. 第 1 家超时后自动进第 2 家  
7. 第 2 家若秒回，保持深聊，第 3 家仍排队  
8. 深聊中再贴第 4 个：提示排在第 N，不打断  
9. 点「优先插队」：当前结束，切到指定项  
10. 卖家同意降价：状态 `done_agreed`，继续下一家  
11. 任何「发送结果无法确认」必须人工打开闲鱼核对，禁止盲目重跑同一条  
