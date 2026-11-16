"""集成自测：对运行中的服务执行关键操作（默认 http://127.0.0.1:8000）。
运行：BASE_URL=http://127.0.0.1:8000 python3 tests/selftest.py
覆盖：两个内置示例全流程、非法语法拒绝、8 份上限、并发升级一致性、HTML 页面冒烟。
"""
import json
import os
import sys
import threading
import uuid

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from schema_spec import message_valid, parse_schema  # noqa: E402
from solver import check_compatibility  # noqa: E402

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8000")
PASS = []


def ok(name, cond, extra=""):
    assert cond, f"FAILED: {name} {extra}"
    PASS.append(name)
    print(f"  ✔ {name}")


def temp_doc(lo, hi, qc=None, qc_required=False):
    props = {"temp": {"type": "integer", "minimum": lo, "maximum": hi},
             "site": {"type": "string", "enum": ["WQ-01", "WQ-02"]}}
    required = ["temp", "site"]
    if qc is not None:
        props["qc"] = {"enum": qc}
        if qc_required:
            required.append("qc")
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


def main():
    c = httpx.Client(base_url=BASE, timeout=30)
    r = c.get("/api/health")
    ok("健康检查", r.status_code == 200 and r.json()["status"] == "ok")

    model = f"t{uuid.uuid4().hex[:10]}"
    r = c.post("/api/models", json={"name": model})
    ok("创建型号", r.status_code == 201, r.text)

    def pub_fw(doc):
        return c.post(f"/api/models/{model}/firmware", json={"schema": doc})

    def pub_pg(pid, doc):
        return c.post(f"/api/models/{model}/programs",
                      json={"program_id": pid, "schema": doc})

    # ---- 示例一：温度扩范围 ----
    print("示例一：温度扩范围")
    r = pub_fw(temp_doc(-10, 50))
    ok("固件v1(-10~50)发布", r.status_code == 200 and r.json()["version"] == 1, r.text)
    r = pub_pg("analyzer", temp_doc(-10, 50))
    ok("程序analyzer旧版发布", r.status_code == 200, r.text)
    r = pub_fw(temp_doc(-40, 100))
    body = r.json()
    ok("固件v2(-40~100)被旧程序拒绝", r.status_code == 409 and not body["ok"], r.text)
    p = body["problems"][0]
    ok("拒绝依据含拒收程序", p["program_id"] == "analyzer", json.dumps(p))
    cex = p["counterexample"]
    ok("反例是合法报文且越旧界", message_valid(cex, parse_schema(temp_doc(-40, 100)))
       and not message_valid(cex, parse_schema(temp_doc(-10, 50))), json.dumps(cex))
    st = c.get(f"/api/models/{model}").json()
    ok("拒绝后已发布格式未变", len(st["firmwares"]) == 1 and len(st["programs"]) == 1)
    r = pub_pg("analyzer", temp_doc(-40, 100))
    ok("程序升级为-40~100", r.status_code == 200, r.text)
    r = pub_fw(temp_doc(-40, 100))
    ok("固件v2再次发布通过", r.status_code == 200 and r.json()["version"] == 2, r.text)

    # ---- 示例二：质控码缺省 ----
    print("示例二：质控码缺省")
    r = pub_pg("strict-qc", temp_doc(-40, 100, qc=["N", "Q"], qc_required=True))
    ok("程序strict-qc(qc必填)被拒", r.status_code == 409, r.text)
    cex = r.json()["problems"][0]["counterexample"]
    ok("反例为qc缺省或null", "qc" not in cex or cex["qc"] is None, json.dumps(cex))
    r = pub_pg("strict-qc", temp_doc(-40, 100, qc=["N", "Q", None]))
    ok("程序strict-qc(qc可选)通过", r.status_code == 200, r.text)
    r = pub_fw(temp_doc(-40, 100, qc=["N", "Q", None]))
    ok("固件v3被未声明qc的analyzer拒收", r.status_code == 409
       and r.json()["problems"][0]["program_id"] == "analyzer", r.text)
    ok("反例含qc字段", "qc" in r.json()["problems"][0]["counterexample"], r.text)
    r = pub_pg("analyzer", temp_doc(-40, 100, qc=["N", "Q", None]))
    ok("analyzer升级为qc可选", r.status_code == 200, r.text)
    r = pub_fw(temp_doc(-40, 100, qc=["N", "Q", None]))
    ok("固件v3(qc可缺省)发布", r.status_code == 200 and r.json()["version"] == 3, r.text)

    # ---- 非法语法拒绝 ----
    print("非法语法")
    bad_docs = [
        {"type": "object", "properties": {"a": {"type": "object", "properties": {}}},
         "additionalProperties": False},
        {"type": "object", "properties": {"a": {"type": "string", "pattern": "^x"}},
         "additionalProperties": False},
        {"type": "object", "properties": {}, "additionalProperties": True},
        {"type": "object", "properties": {"a": {"enum": [True]}}, "additionalProperties": False},
        {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False},
        {"anyOf": [{"type": "string"}]},
    ]
    for i, doc in enumerate(bad_docs):
        r = pub_fw(doc)
        ok(f"非法语法{i+1}被拒(422)", r.status_code == 422 and not r.json()["ok"], r.text)
    st = c.get(f"/api/models/{model}").json()
    ok("非法发布未改状态", len(st["firmwares"]) == 3)

    # ---- 上限：固件至多 8 份 ----
    print("上限检查")
    for lo, hi in [(-40, -30), (-29, -20), (-19, -10), (-9, 0), (1, 10)]:
        r = pub_fw(temp_doc(lo, hi))
        assert r.status_code == 200, r.text
    r = pub_fw(temp_doc(11, 20))
    ok("第9份固件被拒", r.status_code == 422 and "上限" in r.json()["message"], r.text)
    # ---- 上限：程序至多 8 个 ----
    for i in range(2, 8):  # 已有 analyzer、strict-qc 两个，再补 6 个到 8 个
        r = pub_pg(f"prog-{i}", temp_doc(-40, 100, qc=["N", "Q", None]))
        assert r.status_code == 200, r.text
    r = pub_pg("prog-9", temp_doc(-40, 100))
    ok("第9个程序号被拒", r.status_code == 422 and "上限" in r.json()["message"], r.text)
    r = pub_pg("prog-2", temp_doc(-40, 100, qc=["N", "Q", None]))
    ok("替换既有程序不受上限影响", r.status_code == 200, r.text)

    # ---- 日志留痕 ----
    logs = c.get(f"/api/models/{model}/log").json()["logs"]
    ok("日志含拒绝记录", any(l["result"] == "rejected" for l in logs))
    ok("日志含无效记录", any(l["result"] == "invalid" for l in logs))

    # ---- 并发升级：不能各自通过却留下不兼容组合 ----
    print("并发升级一致性")
    m2 = f"c{uuid.uuid4().hex[:10]}"
    assert c.post("/api/models", json={"name": m2}).status_code == 201
    assert c.post(f"/api/models/{m2}/firmware",
                  json={"schema": temp_doc(0, 50)}).status_code == 200
    assert c.post(f"/api/models/{m2}/programs",
                  json={"program_id": "P", "schema": temp_doc(0, 100)}).status_code == 200
    barrier = threading.Barrier(2)
    results = {}

    def narrow_program():  # A: 程序收窄到 0~50（与现有固件兼容）
        barrier.wait()
        r = c.post(f"/api/models/{m2}/programs",
                   json={"program_id": "P", "schema": temp_doc(0, 50)})
        results["A"] = r.status_code

    def widen_firmware():  # B: 固件扩到 0~100（只与旧程序兼容）
        barrier.wait()
        r = c.post(f"/api/models/{m2}/firmware", json={"schema": temp_doc(0, 100)})
        results["B"] = r.status_code

    for _ in range(5):  # 多跑几轮，每轮重建场景
        results.clear()
        barrier = threading.Barrier(2)
        t1 = threading.Thread(target=narrow_program)
        t2 = threading.Thread(target=widen_firmware)
        t1.start(); t2.start(); t1.join(); t2.join()
        ok("并发恰好一方成功", sorted(results.values()) == [200, 409],
           f"A={results.get('A')} B={results.get('B')}")
        st = c.get(f"/api/models/{m2}").json()
        fw_pairs = [(f["version"], parse_schema(f["schema"])) for f in st["firmwares"]]
        pg_pairs = [(p["program_id"], parse_schema(p["schema"])) for p in st["programs"]]
        ok("并发后状态全量兼容", check_compatibility(fw_pairs, pg_pairs) == [])
        # 还原场景：若 A 成功则把程序放宽回 0~100；若 B 成功则状态已含 0~100 固件，
        # 重新发布程序 0~100 使下一轮场景一致
        r = c.post(f"/api/models/{m2}/programs",
                   json={"program_id": "P", "schema": temp_doc(0, 100)})
        assert r.status_code == 200, r.text
        # 若 B 成功过，固件已到 v2；删不了固件（只追加），改用新型号重开
        if results.get("B") == 200:
            m2 = f"c{uuid.uuid4().hex[:10]}"
            assert c.post("/api/models", json={"name": m2}).status_code == 201
            assert c.post(f"/api/models/{m2}/firmware",
                          json={"schema": temp_doc(0, 50)}).status_code == 200
            assert c.post(f"/api/models/{m2}/programs",
                          json={"program_id": "P", "schema": temp_doc(0, 100)}).status_code == 200

    # ---- HTML 页面冒烟 ----
    print("HTML 页面")
    r = c.get("/")
    ok("首页可打开", r.status_code == 200 and "设备型号" in r.text)
    r = c.get(f"/models/{model}")
    ok("型号页可打开", r.status_code == 200 and "历代固件写入格式" in r.text
       and "现用程序读取格式" in r.text and "发布日志" in r.text)
    ok("页面含示例", "温度扩范围" in r.text and "质控码缺省" in r.text)
    r = c.post(f"/models/{model}/firmware",
               data={"schema_text": json.dumps(temp_doc(-40, 100))})
    ok("网页表单发布固件被拒并展示依据", r.status_code == 200
       and "发布被拒绝" in r.text and "反例" in r.text, str(r.status_code))
    r = c.post(f"/models/{model}/programs",
               data={"program_id": "analyzer",
                      "schema_text": json.dumps(temp_doc(-40, 100, qc=["N", "Q", None]))})
    ok("网页表单发布程序成功", r.status_code == 200 and "发布成功" in r.text, str(r.status_code))

    print(f"\n全部 {len(PASS)} 项集成自测通过")
    return model


if __name__ == "__main__":
    used_model = main()
    print(f"MODEL={used_model}")
