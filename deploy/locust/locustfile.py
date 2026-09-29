"""locust 压测（套件 D）：SSE 流式问答 + 轻量 API 混合。

- HttpUser（gevent 阻塞式）：SSE 长连接在绿色线程阻塞读天然让出调度
- sse_ask：client.stream + iter_lines 逐行；confirm 事件行内自动 approve（防 180s 挂起）
- on_start 登录一次（正确密码，绝不登出/错密码——登录防爆破连坐）
- TIER 环境变量：tier50 时 ask 权重降为 4:6（防 50 路同压 default 队列退化性 E005）

用法（经 deploy/run_suite_d.sh）:
    TIER=15 locust -f deploy/locust/locustfile.py --headless -u 15 -r 5 -t 300s \
        --host http://127.0.0.1:24425 --csv=流程测试报告/locust_tier15 --exit-code-on-fail
"""
import json
import os
import time

from locust import HttpUser, between, events, task

from accounts import pick_account, pick_question

TIER = os.environ.get("TIER", "15")
if os.environ.get("MOCK"):
    # 模拟档（2026-08-19）：零 LLM 调用——只压多用户 API 并发（登录/会话/列表/健康）
    ASK_W, LIGHT_W, MULTI_W = 0, 10, 0
elif TIER == "50":
    ASK_W, LIGHT_W, MULTI_W = 4, 6, 1
else:
    ASK_W, LIGHT_W, MULTI_W = 7, 2, 1

E005_COUNT = 0


def sse_ask(user, sid: str, question: str, skills: list[str]) -> tuple[bool, list[str]]:
    """SSE 全流程：逐行读 → question/plan 事件行内应答 → done/error 判定。返回 (ok, error_codes)。

    2026-08-19（v2 契约适配）：confirm 卡已下线（授权卡移除）——question 事件 POST
    /chat/answer（全部默认）、plan 事件 POST /chat/plan-approve（approve）；client.stream
    不存在（HttpSession 无该方法）→ 改 post(stream=True) + 显式 r.close() 释放连接。
    """
    global E005_COUNT
    errs: list[str] = []
    done = False
    ev = None
    with user.client.post("/api/v1/chat/ask", json={
        "session_id": sid, "question": question,
        "active_skills": skills, "auto_skill": False,
    }, headers={"Authorization": f"Bearer {user.token}"}, catch_response=True,
        name="/chat/ask", stream=True) as r:
        if r.status_code >= 400:
            r.failure(f"http_{r.status_code}")
            return False, [f"http{r.status_code}"]
        for line in r.iter_lines():
            if line.startswith(b"event: "):
                ev = line[7:].strip().decode()
                continue
            if line.startswith(b"data: ") and line[6:].startswith(b"{"):
                d = json.loads(line[6:])
                if ev == "question":
                    answers = [{"question_idx": i, "selected": (q.get("recommended") or [0])[:1]}
                               for i, q in enumerate(d.get("questions") or [])]
                    user.client.post("/api/v1/chat/answer", json={
                        "session_id": sid, "question_id": d["question_id"], "answers": answers,
                    }, headers={"Authorization": f"Bearer {user.token}"}, name="/chat/answer")
                elif ev == "plan":
                    user.client.post("/api/v1/chat/plan-approve", json={
                        "session_id": sid, "plan_id": d["plan_id"], "decision": "approve",
                    }, headers={"Authorization": f"Bearer {user.token}"}, name="/chat/plan-approve")
                elif ev == "error":
                    errs.append(d.get("code", "E??"))
                    if d.get("code") == "E005":
                        E005_COUNT += 1
                elif ev == "done":
                    done = True
        if done and not errs:
            r.success()
            return True, []
        r.failure(f"no_done|errors={errs}")
        return False, errs


class AiUser(HttpUser):
    wait_time = between(0.5, 3)
    host = "http://127.0.0.1:24425"

    def on_start(self):
        dept, self.username, password, self.facet = pick_account()
        r = self.client.post("/api/v1/auth/login", json={
            "department_id": dept, "username": self.username, "password": password,
        }, name="/auth/login", catch_response=True)
        if r.status_code != 200:
            r.failure(f"login_{r.status_code}")
            self.interrupt()
        self.token = r.cookies["access_token"]

    def _new_session(self) -> str:
        r = self.client.post("/api/v1/chat/sessions", json={"client_id": f"locust-{self.username}"},
                             headers={"Authorization": f"Bearer {self.token}"}, name="/chat/sessions")
        return r.json()["session_id"]

    @task(ASK_W)
    def ask_simple(self):
        """单轮短问答（按账号功能面轮换问题）。"""
        if self.facet == 'admin':
            return  # admin 无业务会话（/chat/sessions 403），只跑管理端点
        q, skills = pick_question(self.facet)
        sid = self._new_session()
        sse_ask(self, sid, q, skills)

    @task(LIGHT_W)
    def light_api(self):
        """轻量 API 贡献 RPS：会话列表 / 数据时效 / 技能元数据 / 健康。
        2026-08-11：admin 账号访问业务端点恒 403（产品行为）——按角色分派管理端点。"""
        h = {"Authorization": f"Bearer {self.token}"}
        if self.facet == "admin":
            self.client.get("/api/v1/admin/overview", headers=h, name="/admin/overview")
        else:
            self.client.get("/api/v1/chat/sessions", headers=h, name="/chat/sessions")
            self.client.get("/api/v1/dashboard/data-freshness", headers=h, name="/dashboard/data-freshness")
        self.client.get("/api/v1/health", name="/health")

    @task(MULTI_W)
    def multi_round(self):
        """同会话 2 轮：首轮跑完（done）后才发第二轮（任务后台化 409 防御——
        同会话已有 running 任务时新 ask 必 409，08-19 实测）。"""
        if self.facet == 'admin':
            return  # admin 无业务会话（403）
        sid = self._new_session()
        ok, _ = sse_ask(self, sid, "把下面这段要点整理成一段通顺的话：数据平稳、成本下降", ["run_script"])
        if not ok:
            return  # 首轮未完成 → 跳过第二轮（避免必然 409）
        time.sleep(2)  # done 帧后 relay 收尾（unregister）有窗口——立即发第二轮会撞 409
        sse_ask(self, sid, "把上一步结果导成 docx", ["doc_export"])


@events.test_stop.add_listener
def _on_test_stop(environment, **kw):
    """测试结束时报告 E005 总数（locust 不统计应用层 error 事件）。"""
    print(f"\n[locustfile] 应用层 E005（工具队列超时）事件总数: {E005_COUNT}")
