# 并发与阻塞测试（三期专项）

> 目标：验证多用户全面使用平台多个工具时事件循环不阻塞、并发真实生效。
> 背景：全局规则要求"特别注意堵塞和多用户并发的情况"——本目录为独立新增测试套件。

## 脚本

| 脚本 | 内容 |
|---|---|
| `concurrency_test.py` | 15 并发压力测试：**8 正常用户**（查数/表格/画图/文档/知识/沙箱/记忆/搜索）+ **7 抽象用户**（模拟真实用户的奇怪行为：乱操作/坏文件上传/注入攻击/越权访问/空与超长输入/SSE 断连/无效 token 轰炸），并发期间每 1s 打 health 测延迟 |

抽象用户判定：**预期失败（4xx 拒绝/注入拦截/越权 403/断连不崩）视为通过**；仅 500/挂起/应拦截未拦截计为缺陷。

## 运行

```bash
# VSCode 会话须先清理 .venv 污染
cd backend && source scripts/env_aip.sh
python -u tests/concurrency/concurrency_test.py
```

## 判定标准

1. **正常用户**：8 路全部 `done` 且零 SSE `error` 事件
2. **抽象用户**：7 路"预期失败被正确处理"（详见各用例 verdict）
3. **不阻塞**：并发期间 `GET /health` 延迟全部 <1s（>1s 判定事件循环被阻塞）
4. **并发生效**：总耗时 < 串行估计耗时 × 0.7（真实并行而非串行排队）

## 测试账号与数据前提

- 需要演示团队有可查询的数据 + 已注册测试账号（脚本建会话用）
- `/tmp/third_data.xlsx` 测试文件（表格解析用例用；缺失时脚本报错，可自行生成一份示例表格）
- 用例 13（沙箱）与 15（联网搜索）依赖网络/模型可用；失败会单独标记但不拖垮整体判定

## 关联修复（2026-08-05 代码检查发现）

- `doc_tools.run_doc_export`：python-docx/pptx/openpyxl 同步构建 → `asyncio.to_thread`
- `import_pipeline.parse_workbook`：openpyxl 同步解析 → `asyncio.to_thread`
- 上传写盘三处（upload/kb_service/chat）→ `asyncio.to_thread`
