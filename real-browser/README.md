# real-browser · 真机浏览器驱动

给智能体驱动的**真实浏览器**：以 MCP（Model Context Protocol）暴露一套"看一眼 → 动一下 → 再看一眼"
的操作原语，让 agent 能在真实浏览器里完成网页任务。

**定位是通用驱动层**：不绑定站点、不绑定业务。换站点不加代码，加一份**站点档案**即可——
`<home>/profiles/<名>.json` 里放节奏、登录判据、列表选择器，都是数据不是逻辑。

---

## 三条设计原则

1. **用真机，不伪造身份**：不设 UA、不碰指纹、不覆盖 locale / timezone——本机是什么就是什么。
   身份的真实性来自环境本身，额外覆盖反而制造矛盾。
2. **不开任何调试端口**：走管道驱动，进程不监听任何 TCP 端口。
3. **护栏挂在工具上**：导航间隔、URL 闸、判死信号、审计内建在工具层，agent 看不见也绕不过。
   不设"每小时多少次"这类硬配额——那是规模问题，硬拦会误伤正常使用；规模约束写进提示词，
   工具只保证"想快也快不了"。

---

## 工具面（19 个）

| 类别 | 工具 |
|---|---|
| 原子操作 | `browse_open` `browse_snapshot` `browse_click` `browse_type` `browse_scroll` `browse_press` `browse_back` `browse_text` `browse_screenshot` `browse_download` |
| 等待/就绪 | `browse_wait`（"导航返回"≠"内容就绪"，用这个等目标内容，有界，超时如实报告） |
| 站点经验 | `browse_remember`（按域名记一条**验证过的**事实；下次到该站点自动顶在快照开头） |
| 开放读取（只读） | `browse_eval` `browse_extract` |
| 人机交接 | `browse_login_status` `browse_ask_human` `browse_resume` `browse_takeover` |
| 收尾 | `browse_close` |

**契约**：写动作必返新快照 + 状态摘要（URL / 列表条数变化）；
`browse_eval` / `browse_extract` **只能读**，不能用来点击或输入——脚本合成的事件
`isTrusted=false`，站点一眼就能认出。

**基本循环**：`browse_open(网址)` → 返回带编号 `[n]` 的页面快照 → `browse_click(n)` / `browse_type(n, 文本)`。
编号在每次快照后刷新：页面一变就必须重新 `browse_snapshot` 取号，拿旧编号点会点到别的地方。

---

## 目录

```
agent_browser/          驱动层
  config.py             目录定位与参数（跨平台）
  identity.py           身份档（一账号一 profile）
  launcher.py           浏览器启动（真机参数，不做环境覆盖）
  session.py            会话与登录态持久化
  profiles.py           站点档案加载
  humanize.py           拟人化节奏
  snapshot.py           页面快照与编号
  evaluate.py           只读脚本执行（隔离世界）
  guard.py              导航间隔 / URL 闸 / 判死信号
  notes.py              站点经验（按域名记事实）
  ipc.py                本地进程间通信
  browser_agent.py      工具实现主体
  daemon.py             常驻守护进程
  client.py             守护进程客户端
  mcp_server.py         MCP 工具面
tests/                  自检脚本 + 页面夹具
```

---

## 进程模型：浏览器由常驻守护进程持有

```
宿主程序 ──拉起/回收──▶ MCP 服务进程（agent_browser.mcp_server，瘦客户端）
                            │ 本地 IPC（Unix socket，不开任何 TCP 端口）
                            ▼
                    守护进程（agent_browser.daemon，常驻，一身份一进程）
                            └── 浏览器窗口（profile 锁也归它持有）
```

**为什么要有守护进程**：宿主程序会在回合之间回收 MCP 服务进程。浏览器若由那个进程持有，
进程一没、窗口就跟着没——而"人机交接"（例如让用户扫码登录）**恰恰要求窗口在回合之间活着**。
交给守护进程持有后，被回收的只是"话筒"：窗口、登录态、当前页面都在，下次连上来接着用；
只有 `browse_close`（或用户手动关窗）才真正收摊。

守护进程起不来时会**自动退回进程内模式**（功能一样，只是窗口扛不住进程回收），并写日志。

- 守护进程日志：`<home>/logs/daemon-<身份>.log`（排障先看它）
- 前台手工跑（日志直接打屏幕）：
  `python -m agent_browser.daemon --identity <身份名>`

### 登录态为什么能跨窗口保留

浏览器退出时会丢掉**会话级 cookie**，而"登录一次、以后都在"恰恰靠它们。
所以本驱动在关窗前把 context 的**全部** cookie（含会话级）落盘到
`<home>/sessions/<身份>.cookies.json`（权限 0600），下次开窗前灌回去；守护进程每 60 秒也顺手存一次。

> **注意这只解决"cookie 丢失"**。如果站点本身**同一账号只允许一个网页端在线**，
> 那"用户自己登着 + agent 也登着"仍会互相顶下线——那是产品约定问题不是技术问题。
> **不要试图从用户日常浏览器的 profile 里取凭据**：既是"偷票据"的形态，技术上也被
> 新版 Chromium 的 App-Bound Encryption 挡住（浏览器之外的进程解不开）。

### 两种资料目录模式

接入时用 `--profile-mode` 选：

- **`dedicated`（默认）**：本驱动专属 profile（`<home>/profiles/<身份>`）。与用户日常浏览器的
  登录态、书签、历史完全隔离；代价是**第一次要在 agent 窗口里扫码**（之后靠 cookie 存/恢复）。
- **`user`**：直接用**用户日常浏览器的资料目录**。好处是零扫码、直接就是他登着的账号。
  代价：Chromium 同一资料目录同时只能一个实例（OS 级进程单例），所以工具不硬抢——
  检测到用户浏览器在跑就返回说明，由用户决定"自己关"还是"让 agent 帮关"（`browse_takeover`，
  只发关闭信号、不强制结束）。

---

## 接到灵枢平台

平台侧以 **MCP Streamable HTTP** 接入，「MCP 工具」页里的条目 id 为 `browse`：

```bash
# 平台「MCP 工具」条目指向这个地址
python -m agent_browser.mcp_server \
  --identity tardis --transport streamable-http --host 127.0.0.1 --port 18130
```

平台后端经 `services/mcp_client.py` 连接，启动时自动发现 `tools/list` 并把 19 个子工具
注册成 `group="mcp"` 的工具（以「运维启用 ∧ 用户个人启用」双闸门控制可见性）。

**收尾三层**：智能体主动调 `browse_close`（优先）→ 空闲 300 秒自动关闭 → 手工关闭。

---

## 自检

```bash
python tests/smoke.py           # 全链路：打开 → 快照 → 点击 → 输入 → 新标签跟随 …
python tests/daemon_smoke.py    # 守护进程：跨进程复用窗口与登录态
python tests/test_mcp.py        # MCP 联通与工具清单
```

浏览器首次冷启动需要 10–20 秒，之后复用守护进程是秒级。演示或回归前建议先跑一次预热。

---

## 已知坑

1. **hover 有副作用的元素**（下拉、带预览卡片）：用 `browse_click(ref, path="direct")` 或少掠过。
2. **截屏在锁屏时只能拿到黑屏**（Chromium 在会话锁定时不合成）：跑自检请在解锁状态。
3. **中文目录名不要跨 WSL↔Windows 传参**：给程序的参数用 Windows 形式（`E:\...`），
   给内核 exec 的可执行文件用 `/mnt/e/...` 形式——两个方向不一样。
4. **无头模式的窗口几何会自相矛盾**：`--window-size` 可能把 outer 变成 0×0；
   必要时用 `--ozone-override-screen-size` 兜底。
