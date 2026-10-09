"""网站体检/红队自检：功能测试全过、安全测试把关、LLM 点评、网页与 PDF。"""
import json

from fastapi.testclient import TestClient

from desk import qa
from tests.test_web import env  # noqa: F401


def fake_qa_llm(model, system, user):
    out = {"verdict": "整体没大问题，有几处可改。",
           "findings": [{"severity": "低", "what": "首页信息略多", "fix": "折叠次要内容"}]}
    return json.dumps(out, ensure_ascii=False), 400, 120


def test_functional_and_security_all_pass(env, monkeypatch):  # noqa: F811
    s, web = env
    # qa 模块通过 desk.web.app / desk.web.S 工作；本测试里它们就是 env 这套
    monkeypatch.setattr(qa, "_client", lambda: (TestClient(web.app, base_url="http://testserver"), web))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rec = qa.run(s, push=False)
    assert rec["n"] >= 20
    fails = [c for c in rec["checks"] if not c["ok"]]
    assert not fails, [f["name"] + ":" + f["detail"] for f in fails]
    # 关键安全项确实在被测
    names = {c["name"] for c in rec["checks"]}
    assert any("锁定" in n for n in names) and any("XSS" in n for n in names) and any("泄露密钥" in n for n in names)
    assert any("提示注入" in n or "指令" in n for n in names)


def test_llm_review_and_pages(env, monkeypatch):  # noqa: F811
    s, web = env
    monkeypatch.setattr(qa, "_client", lambda: (TestClient(web.app, base_url="http://testserver"), web))
    rec = qa.run(s, llm=fake_qa_llm, push=False)
    assert set(rec["review"]) == {a["id"] for a in qa.AGENTS}
    assert rec["review"]["RED_SOCIAL"]["verdict"]
    c = TestClient(web.app)
    c.post("/login", data={"username": "kea", "password": "correct horse battery"})
    r = c.get("/qa")
    assert r.status_code == 200
    for t in ("测试与安全自检", "红队-技术", "红队-社工", "逐项结果", "界面流程测试员"):
        assert t in r.text, t
    pdf = c.get(f"/qa/{rec['run_id']}.pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF" and len(pdf.content) > 2500
    assert c.get("/qa/nope.pdf").status_code == 404


def test_qa_run_button(env, monkeypatch):  # noqa: F811
    s, web = env
    calls = []
    monkeypatch.setattr(qa, "run", lambda *a, **k: calls.append(1))
    import desk.web as W
    W._last_qa_run[0] = 0
    c = TestClient(web.app)
    c.post("/login", data={"username": "kea", "password": "correct horse battery"})
    r = c.post("/qa/run", headers={"origin": "http://testserver", "host": "testserver"}, follow_redirects=False)
    assert r.status_code == 303
    # 外站来源被拒
    assert c.post("/qa/run", headers={"origin": "https://evil.x", "host": "testserver"}).status_code == 403
