# OpenQQWaifu 项目 Bug 审计报告与修复计划

> 审计时间：2026-04-15  
> 审计范围：`src/waifu_standalone/` 下全部 Python 源码（约 30 个模块、~6000 行）

---

## 总览

| 严重等级 | 数量 | 说明 |
|---------|------|------|
| 🔴 高危 | 4 | 可导致数据丢失、安全隐患或运行时崩溃 |
| 🟡 中等 | 6 | 可导致功能异常或逻辑错误 |
| 🟢 低危 | 4 | 代码质量/性能问题，不立即影响正确性 |

---

## 🔴 高危 Bug

### BUG-01：`_session_knowledge_entries` 类型不匹配——向 list 追加 tuple

**文件**：[app.py](file:///c:/openqqwaifu/src/waifu_standalone/app.py#L837)  
**位置**：第 837 行  
**描述**：`filtered` 声明为 `list[dict[str, Any]]`，但实际执行 `filtered.append((score, dict(entry)))`——追加的是 **tuple**，不是 dict。  
后续排序 lambda `item[0]`、`item[1]` 虽然碰巧能在 tuple 上工作（`item[0]` 是 score，`item[1]` 是 dict），但返回表达式 `[entry for _, entry in filtered[...]]` 也说明开发者意识到了它其实是 tuple 列表。类型注解与实际结构不符，在严格类型检查 / 未来重构中会引发 runtime 异常。

**影响**：如果有下游代码以 `dict` 方式访问 `filtered` 的元素，会直接抛出 `TypeError`。目前定义和使用恰好配合，但属于类型安全炸弹。

**修复建议**：
```diff
- filtered: list[dict[str, Any]] = []
+ scored: list[tuple[float, dict[str, Any]]] = []
  ...
- filtered.append((score, dict(entry)))
+ scored.append((score, dict(entry)))
  ...
- filtered.sort(...)
- return [entry for _, entry in filtered[...]]
+ scored.sort(...)
+ return [entry for _, entry in scored[...]]
```

---

### BUG-02：`recall_knowledge` 全表扫描——SQLite 大表性能炸弹

**文件**：[state_store.py](file:///c:/openqqwaifu/src/waifu_standalone/state_store.py#L806-L812)  
**位置**：`SqliteRuntimeStateStore.recall_knowledge`，第 806–812 行  
**描述**：每次召回知识都执行 `SELECT * FROM knowledge_entries ORDER BY ...` **没有 WHERE 子句，没有 LIMIT**，将整张表全部加载到内存后再在 Python 中过滤 scope 和评分。当知识条目积累到数千条时，这个操作的开销将线性增长，且会阻塞 `self._lock`，影响所有并发写入。

**影响**：
- 消息回复延迟随数据量线性增大
- 在锁持有期间 blocking 所有其他 state_store 操作

**修复建议**：
```sql
-- 在 SQL 层面先按 scope 过滤
SELECT * FROM knowledge_entries
WHERE (scope_type, scope_id) IN (...)
ORDER BY updated_at DESC, id DESC
LIMIT ?
```
并将评分逻辑移到取出后（已有 limit 保护）。

---

### BUG-03：搜索缓存无限增长——内存泄漏

**文件**：[searching.py](file:///c:/openqqwaifu/src/waifu_standalone/systems/searching.py#L87-L131)  
**位置**：`SearchDecider._cache` 字段  
**描述**：`_cache: dict[str, SearchContext]` **只进不出**，每次新 query 都写入缓存。在长期运行的服务中，缓存永远不会被清除或淘汰，随时间无限增长。

**影响**：长期运行会导致 Python 进程内存占用持续攀升，最终可能 OOM。

**修复建议**：
- 选项 A：加 `maxsize` 限制，达到上限后淘汰最旧的 key
- 选项 B：加 TTL，超过一定时间的条目自动过期
- 选项 C：使用 `functools.lru_cache` 或类似的 bounded cache

---

### BUG-04：敏感配置信息通过 API 明文返回

**文件**：
- [app.py](file:///c:/openqqwaifu/src/waifu_standalone/app.py#L1376-L1416) (`get_ai_panel` / `save_ai_panel`)
- [app.py](file:///c:/openqqwaifu/src/waifu_standalone/app.py#L1630-L1672) (`get_sidecar_panel`)
- [app.py](file:///c:/openqqwaifu/src/waifu_standalone/app.py#L1680-L1703) (`get_qq_login_panel`)

**描述**：`get_ai_panel()` 返回 `api_key` 明文，`get_sidecar_panel()` 返回 `access_token` 和 `webui_token` 明文。这些敏感信息通过 HTTP API 直接暴露给前端。

**影响**：任何能访问 Web 控制台的人都可以直接看到所有 API 密钥。在多用户环境下属于严重安全隐患。

**修复建议**：
```python
# 在返回前 mask 敏感字段
def _mask_key(key: str) -> str:
    if not key or len(key) < 8:
        return "***"
    return key[:4] + "..." + key[-4:]
```
并在各 panel 返回时对 `api_key`、`access_token`、`webui_token` 做 mask。

---

## 🟡 中等 Bug

### BUG-05：`image_command_aliases` 默认值包含与 `image_command_prefix` 重复的条目

**文件**：[config.py](file:///c:/openqqwaifu/src/waifu_standalone/config.py#L110-L111)  
**位置**：第 110–111 行  
**描述**：
```python
image_command_prefix: str = "生图"
image_command_aliases: list[str] = field(default_factory=lambda: ["生图", "draw"])
```
`image_command_prefix` 已经是 `"生图"`，`image_command_aliases` 又包含 `"生图"`。虽然 `_image_command_prefixes()` 内部做了去重，但默认配置本身就有冗余，容易让用户困惑（以为 aliases 应该只放额外的别名）。

**修复建议**：
```diff
- image_command_aliases: list[str] = field(default_factory=lambda: ["生图", "draw"])
+ image_command_aliases: list[str] = field(default_factory=lambda: ["draw"])
```

---

### BUG-06：`_emit_message` 中 `time.sleep` 阻塞工作线程

**文件**：[app.py](file:///c:/openqqwaifu/src/waifu_standalone/app.py#L418-L421)  
**位置**：第 419–421 行  
**描述**：
```python
if event.launcher_type == "group":
    delay = max(0.0, float(self.config.group_response_delay_seconds))
    if delay > 0:
        time.sleep(delay)
```
在 `ThreadingHTTPServer` 驱动的线程中直接 sleep，会阻塞当前线程。当有大量群消息并行到达时，线程池可能被 sleep 操作耗尽。

**影响**：高并发场景下可能导致新请求排队等待，增加响应延迟。

**修复建议**：短期内记录这是一个已知限制，长期考虑改用异步延迟或将消息推入队列后异步发送。

---

### BUG-07：`_fallback_summary` 可能产生 `IndexError`

**文件**：[generator.py](file:///c:/openqqwaifu/src/waifu_standalone/cells/generator.py#L495-L500)  
**位置**：第 498 行  
**描述**：
```python
def _fallback_summary(self, history_lines: list[str]) -> tuple[str, list[str]]:
    cleaned = [line.strip() for line in history_lines if line.strip()]
    preview = "；".join(cleaned[:3])
    summary = self._clip(preview or cleaned[0], limit=60)  # ← 这里
```
当 `preview` 为空字符串且 `cleaned` 也为空列表时，`cleaned[0]` 会抛出 `IndexError`。虽然上层 `summarize_history` 在 `history_lines` 为空时提前返回，但如果传入的行全部是空白字符串，`cleaned` 就会为空。

**修复建议**：
```diff
- summary = self._clip(preview or cleaned[0], limit=60)
+ summary = self._clip(preview or (cleaned[0] if cleaned else ""), limit=60)
```

---

### BUG-08：`_resolve_address` 返回英文 `"you"` 而非中文

**文件**：[app.py](file:///c:/openqqwaifu/src/waifu_standalone/app.py#L2093)  
**位置**：第 2093 行  
**描述**：`WaifuService._resolve_address()` 的 fallback 返回 `"you"`：
```python
return event.sender_name or "you"
```
但 `Generator._resolve_address()` 在 [generator.py:558](file:///c:/openqqwaifu/src/waifu_standalone/cells/generator.py#L558) 返回的是 `"你"`。两处语义一致的方法有不同的 fallback 值。整个项目的对话语气是中文，返回英文 `"you"` 明显是编码遗留错误。

**修复建议**：
```diff
- return event.sender_name or "you"
+ return event.sender_name or "你"
```

---

### BUG-09：`_count_group_members` 全量扫描 member 列表

**文件**：[app.py](file:///c:/openqqwaifu/src/waifu_standalone/app.py#L2296-L2304)  
**位置**：第 2299 行  
**描述**：
```python
members = self.state_store.list_members(limit=5000)
```
这个方法在 `list_sessions` 中对每个 session 都被调用。假设有 20 个 session，就会对 state_store 做 20 × 5000 条成员数据的全量扫描。在 SQLite 后端中，这意味着每次打开 dashboard 都会发起多次 `SELECT * FROM members LIMIT 5000`。

**影响**：Dashboard 加载速度随成员数线性退化。

**修复建议**：为 `state_store` 增加 `count_members_in_group(group_id: str)` 方法，在 SQL 层面做 `SELECT COUNT(*) WHERE group_id = ?`。

---

### BUG-10：`_knowledge_count_for_session` 全量拉取再 Python 内计数

**文件**：[app.py](file:///c:/openqqwaifu/src/waifu_standalone/app.py#L2208-L2236)  
**位置**：第 2209 行  
**描述**：
```python
total = max(1, int(self.state_store.knowledge_count()))
entries = self.state_store.list_knowledge(limit=total)
```
先获取总数，然后用总数作为 limit 拉取全部知识条目到内存，再在 Python 中过滤。与 BUG-09 类似，在 `list_sessions` 中被反复调用。

**修复建议**：增加 `count_knowledge_for_scopes(scopes)` SQL 查询方法。

---

## 🟢 低危 Bug / 代码质量问题

### BUG-11：`Generator.__init__` 私有创建 `CardManager` 导致实例脱节

**文件**：[generator.py](file:///c:/openqqwaifu/src/waifu_standalone/cells/generator.py#L20)  
**位置**：第 20 行  
**描述**：`Generator.__init__` 内部自建 `self._cards = CardManager(config)`。但 `_build_service` 中通过 `cards = generator._cards` 拿到引用后直接共享。

```python
generator = Generator(app_config)
cards = generator._cards  # 直接引用内部私有字段
```

当 `_refresh_runtime_components(rebuild_generator=True)` 重建 generator 时：
```python
self.generator = Generator(self.config)
self.cards = self.generator._cards
```
这样重建后**新的 cards 实例**会替换 service 上的引用，但如果有任何其他地方缓存了旧的 cards 引用，就会出现脱节。

**修复建议**：让 `CardManager` 从外部注入到 `Generator`，避免访问 `_` 前缀的内部实现。

---

### BUG-12：`save_abilities_panel` 中 `bool(payload.get(...))` 对 `False` 值的处理不正确

**文件**：[app.py](file:///c:/openqqwaifu/src/waifu_standalone/app.py#L1531-L1556)  
**位置**：第 1535–1556 行  
**描述**：以下模式：
```python
self.config.thinking_mode = bool(payload.get("thinking_mode", self.config.thinking_mode))
```
当用户明确传入 `"thinking_mode": false`（JSON false）时，`payload.get("thinking_mode", ...)` 返回 `False`，`bool(False)` = `False`，这种情况**碰巧是对的**。

但对于数值型字段，模式如：
```python
self.config.search_result_limit = int(payload.get("search_result_limit", ...) or ...)
```
当用户传入 `0` 时，`int(0 or self.config.search_result_limit)` 会走到 fallback，**忽略用户明确设置为 0 的意图**。虽然大部分字段值为 0 不合理，但这是一个潜在的逻辑陷阱。

**修复建议**：
```python
# 正确处理 explicit None vs falsy value
raw = payload.get("search_result_limit")
if raw is not None:
    self.config.search_result_limit = max(1, int(raw))
```

---

### BUG-13：`_ensure_column` 存在 SQL 注入风险

**文件**：[state_store.py](file:///c:/openqqwaifu/src/waifu_standalone/state_store.py#L538-L544)  
**位置**：第 540、544 行  
**描述**：
```python
columns = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
```
`table_name`、`column_name` 和 `definition` 直接插入到 SQL 字符串中。虽然当前所有调用都使用硬编码的值（如 `"members"`、`"affinity_score"`），不存在实际攻击面，但作为通用方法，如果未来被复用于用户输入场景，就会构成 SQL 注入漏洞。

**修复建议**：增加参数校验或使用白名单模式。

---

### BUG-14：HTTP OneBot 回调始终返回 `204 NO_CONTENT`，丢弃回复信息

**文件**：[http_api.py](file:///c:/openqqwaifu/src/waifu_standalone/http_api.py#L650-L660)  
**位置**：第 654–660 行  
**描述**：
```python
status, body = api.handle_json(payload)
if status < HTTPStatus.BAD_REQUEST:
    self.send_response(HTTPStatus.NO_CONTENT)
    self.end_headers()
    return
```
`handle_json` 返回了 `(200, {reply: ...})` 但 HTTP handler 将成功状态一律覆盖为 `204 No Content`，且不带 body。虽然 OneBot 协议通常不依赖 HTTP response 来传递回复（回复是通过主动调用 API 发送的），但如果有客户端期望通过 HTTP response 获取回复结果，这里的逻辑就不正确。

**影响**：如果 `handle_json` 的返回值有意义（比如调用方需要知道消息是否成功处理），当前实现总是丢弃它。

**修复建议**：确认是否有调用者依赖 HTTP response body。如果没有，添加注释说明设计意图；如果有，改为返回实际 body。

---

## 修复优先级排序

| 优先级 | Bug ID | 描述 | 预估工作量 |
|--------|--------|------|-----------|
| P0 | BUG-02 | SQLite 全表扫描性能炸弹 | 中 (~2h) |
| P0 | BUG-03 | 搜索缓存无限增长内存泄漏 | 低 (~30min) |
| P0 | BUG-04 | API 密钥明文泄露 | 低 (~1h) |
| P1 | BUG-01 | 类型不匹配的 tuple/dict 问题 | 低 (~15min) |
| P1 | BUG-07 | `_fallback_summary` IndexError | 低 (~10min) |
| P1 | BUG-08 | 中英文 fallback 不一致 | 低 (~5min) |
| P2 | BUG-05 | 默认配置冗余 | 低 (~5min) |
| P2 | BUG-06 | `time.sleep` 阻塞线程 | 高 (~4h，需架构调整) |
| P2 | BUG-09 | Dashboard 成员全表扫描 | 中 (~1h) |
| P2 | BUG-10 | 知识条目全量拉取 | 中 (~1h) |
| P3 | BUG-11 | CardManager 内部耦合 | 中 (~2h) |
| P3 | BUG-12 | 数值 0 被误判为 falsy | 中 (~1h) |
| P3 | BUG-13 | `_ensure_column` SQL 拼接 | 低 (~30min) |
| P3 | BUG-14 | OneBot 回调 204 覆盖 | 低 (~15min) |

---

## 其他观察（非 Bug，但值得关注）

1. **app.py 体积过大**：2431 行，涵盖事件处理、面板CRUD、配置持久化、工厂函数等，建议拆分。
2. **线程模型**：使用 `ThreadingHTTPServer` + per-session 锁，在高并发下可能成为瓶颈。OneBot 消息和 web console 请求共享同一个服务器实例。
3. **无依赖管理**：`pyproject.toml` 中没有声明任何 runtime 依赖，但代码中 import 了多个标准库之外可能隐含的包（需确认）。
4. **Python 版本**：`requires-python = ">=3.11"`，但测试环境实际使用 Python 3.10，可能导致部分 3.11+ 特性不可用。
5. **`FileMemoryStore` 无并发保护**：多线程并发写同一个 session 文件时，可能发生数据竞争（虽然 session lock 理论上限制了这种情况）。
