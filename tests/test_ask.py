"""havn ask: questions answered through the semantic layer.

The language model is replaced by a fake provider that returns canned
decisions and records what it was sent, so these tests pin the contract: the
model only picks from the catalog, havn validates and compiles, and no row
data reaches the model unless the project opts in.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from havn.engine.ai.ask import AskContext, ask, suggest_chart
from havn.engine.ai.config import AIConfig, AIConfigError, parse_ai_config
from havn.engine.ai.evaluate import compare, expand_paths, load_cases, run_eval
from havn.engine.ai.providers import (
    AnthropicProvider,
    LLMProvider,
    OpenAICompatibleProvider,
    ProviderError,
    extract_json_object,
)
from havn.engine.ai.spec import QuerySpec, SpecError, compile_spec, validate_spec
from havn.engine.semantic import SemanticError, compile_metric, get_metric, load_metrics

METRICS_YML = """metrics:
  - name: revenue
    description: Total order revenue
    model: gold.orders
    measure: SUM(amount)
    dimensions: [region, country]
    time_dimension: order_date
    filters:
      - status != 'cancelled'
  - name: order_count
    description: Number of orders
    model: gold.orders
    measure: COUNT(*)
    dimensions: [region, country]
    time_dimension: order_date
  - name: refunds
    description: Refunded amount
    model: gold.refunds
    measure: SUM(refund)
    dimensions: [region]
    time_dimension: refund_date
"""

SECRET_VALUE = "zz-secret-customer"


def _query(spec: dict, explanation: str = "") -> dict:
    return {
        "kind": "query", "spec": spec, "explanation": explanation,
        "closest_metrics": [], "suggested_metric": None, "clarification": None,
    }


def _unanswerable(closest=None, suggestion=None, explanation="no metric for that") -> dict:
    return {
        "kind": "unanswerable", "spec": None, "explanation": explanation,
        "closest_metrics": closest or [], "suggested_metric": suggestion, "clarification": None,
    }


class FakeProvider(LLMProvider):
    """Returns queued decisions; records every call."""

    name = "fake"
    model = "fake-1"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete_json(self, *, system, messages, schema=None):
        self.calls.append({"system": system, "messages": messages, "schema": schema})
        if not self.responses:
            raise ProviderError("no more canned responses")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r(system, messages) if callable(r) else r

    @property
    def sent_text(self) -> str:
        return json.dumps(self.calls)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "project.yml").write_text(
        "name: asktest\ndatabase:\n  path: warehouse.duckdb\n"
        "ai:\n  provider: openai\n  base_url: http://localhost:11434/v1\n  model: local-test\n"
    )
    for d in ("bronze", "gold"):
        (tmp_path / "transform" / d).mkdir(parents=True)
    (tmp_path / "transform/bronze/orders.sql").write_text(
        "@config materialized=table\n"
        "SELECT id, region, country, amount, status, order_date, customer FROM landing.orders\n"
    )
    (tmp_path / "transform/gold/orders.sql").write_text(
        "@config materialized=table\n@col region: Sales region\n"
        "SELECT * FROM bronze.orders\n"
    )
    (tmp_path / "transform/gold/refunds.sql").write_text(
        "@config materialized=table\n"
        "SELECT region, refund, refund_date FROM landing.refunds\n"
    )
    (tmp_path / "metrics").mkdir()
    (tmp_path / "metrics/sales.yml").write_text(METRICS_YML)
    conn = duckdb.connect(str(tmp_path / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA landing")
    conn.execute(f"""
        CREATE TABLE landing.orders AS SELECT * FROM (VALUES
            (1, 'north', 'NO', 100.0, 'paid', DATE '2026-01-05', '{SECRET_VALUE}'),
            (2, 'north', 'SE', 50.0, 'paid', DATE '2026-02-10', 'b'),
            (3, 'south', 'NO', 70.0, 'cancelled', DATE '2026-02-11', 'c'),
            (4, 'south', 'DK', 30.0, 'paid', DATE '2026-03-01', 'd')
        ) t(id, region, country, amount, status, order_date, customer)
    """)
    conn.execute("""
        CREATE TABLE landing.refunds AS SELECT * FROM (VALUES
            ('north', 10.0, DATE '2026-01-20'), ('east', 5.0, DATE '2026-02-02')
        ) t(region, refund, refund_date)
    """)
    from havn.engine.transform import run_transform

    run_transform(conn, tmp_path / "transform", project_dir=tmp_path)
    conn.close()
    return tmp_path


def _ctx(project: Path, provider, *, ai: AIConfig | None = None, role: str = "admin", conn=None):
    from havn.config import load_project
    from havn.engine.transform import discover_all_models

    config = load_project(project)
    return AskContext(
        project_dir=project,
        conn=conn,
        user={"username": "t", "role": role},
        provider=provider,
        ai=ai or AIConfig(provider="openai", model="x", base_url="http://localhost:1/v1"),
        project_config=config,
        models=discover_all_models(project, config),
    )


@pytest.fixture
def conn(project):
    c = duckdb.connect(str(project / "warehouse.duckdb"), read_only=True)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Semantic layer additions: query-time filters and ordering
# ---------------------------------------------------------------------------


class TestCompileFilters:
    def test_where_and_order(self, project):
        m = get_metric(project, "revenue")
        sql = compile_metric(
            m, dimensions=["region"],
            where=[{"dimension": "country", "op": "in", "value": ["NO", "SE"]},
                   {"dimension": "region", "op": "!=", "value": "o'brien"}],
            order_by=[("revenue", True)], limit=5,
        )
        assert "\"country\" IN ('NO', 'SE')" in sql
        assert "\"region\" != 'o''brien'" in sql
        assert 'ORDER BY "revenue" DESC' in sql
        assert "(status != 'cancelled')" in sql

    def test_filter_on_undeclared_dimension_rejected(self, project):
        m = get_metric(project, "revenue")
        with pytest.raises(SemanticError, match="cannot filter"):
            compile_metric(m, where=[{"dimension": "customer", "op": "=", "value": "x"}])

    def test_injection_shaped_values_stay_literals(self, project, conn):
        m = get_metric(project, "revenue")
        sql = compile_metric(m, where=[{"dimension": "region", "op": "=", "value": "x' OR 1=1 --"}])
        assert conn.execute(sql).fetchall() == [(None,)]

    def test_null_and_numbers(self, project):
        m = get_metric(project, "revenue")
        sql = compile_metric(m, where=[{"dimension": "region", "op": "=", "value": None},
                                       {"dimension": "country", "op": "!=", "value": 3}])
        assert '"region" IS NULL' in sql and '"country" != 3' in sql
        with pytest.raises(SemanticError):
            compile_metric(m, where=[{"dimension": "region", "op": "like", "value": "x"}])
        with pytest.raises(SemanticError):
            compile_metric(m, order_by=[("customer", True)])


# ---------------------------------------------------------------------------
# QuerySpec
# ---------------------------------------------------------------------------


class TestSpec:
    def test_lenient_parse_and_names_resolve(self, project):
        metrics, _ = load_metrics(project)
        spec = QuerySpec.from_dict({"metric": "Revenue", "group_by": ["REGION"], "time_grain": "month",
                                    "order_by": "revenue desc", "limit": "3"})
        assert validate_spec(spec, metrics) == []
        assert spec.metrics == ["revenue"] and spec.dimensions == ["region"] and spec.limit == 3

    def test_errors_are_collected(self, project):
        metrics, _ = load_metrics(project)
        spec = QuerySpec.from_dict({"metrics": ["revenue", "nope"], "dimensions": ["customer"],
                                    "grain": "fortnight", "start": "last month"})
        errors = validate_spec(spec, metrics)
        assert any("unknown metric 'nope'" in e for e in errors)
        assert any("customer" in e for e in errors)
        assert any("grain" in e for e in errors)
        assert any("ISO date" in e for e in errors)

    def test_bad_shape(self):
        with pytest.raises(SpecError):
            QuerySpec.from_dict({"metrics": "revenue", "limit": "many"})
        with pytest.raises(SpecError):
            QuerySpec.from_dict(["revenue"])

    def test_multi_metric_join(self, project, conn):
        metrics, _ = load_metrics(project)
        spec = QuerySpec.from_dict({"metrics": ["revenue", "refunds"], "dimensions": ["region"]})
        assert validate_spec(spec, metrics) == []
        rows = conn.execute(compile_spec(spec, metrics)).fetchall()
        assert rows == [("east", None, 5.0), ("north", 150.0, 10.0), ("south", 30.0, None)]

    def test_multi_metric_needs_shared_dimension(self, project):
        metrics, _ = load_metrics(project)
        spec = QuerySpec.from_dict({"metrics": ["revenue", "refunds"], "dimensions": ["country"]})
        assert any("refunds" in e for e in validate_spec(spec, metrics))

    def test_single_metric_matches_metrics_query(self, project):
        metrics, _ = load_metrics(project)
        spec = QuerySpec.from_dict({"metrics": ["revenue"], "dimensions": ["region"], "grain": "month"})
        assert compile_spec(spec, metrics) == compile_metric(
            metrics["revenue"], dimensions=["region"], grain="month"
        )


# ---------------------------------------------------------------------------
# ask()
# ---------------------------------------------------------------------------


class TestAsk:
    def test_answer_carries_spec_sql_lineage_freshness(self, project, conn):
        provider = FakeProvider(_query({"metrics": ["revenue"], "dimensions": ["region"]}, "Revenue per region"))
        out = ask("revenue by region", _ctx(project, provider, conn=conn))
        assert out["status"] == "answered" and out["verified"] is True
        assert out["result"]["rows"] == [["north", 150.0], ["south", 30.0]]
        assert out["spec"]["metrics"] == ["revenue"]
        assert out["sql"].startswith("SELECT")
        assert out["metrics"][0]["measure"] == "SUM(amount)"
        assert out["models"] == ["gold.orders"]
        lineage = out["lineage"][0]
        names = [n["name"] for n in lineage["nodes"]]
        assert names[:2] == ["gold.orders", "bronze.orders"]
        assert lineage["sources"] == ["landing.orders"]
        fresh = {f["model"]: f for f in out["freshness"]}
        assert set(fresh) == {"gold.orders", "bronze.orders"}
        assert fresh["gold.orders"]["is_stale"] is False and fresh["gold.orders"]["last_run_at"]
        assert out["chart"] == {"type": "bar", "x": "region", "y": ["revenue"], "series": None}

    def test_stale_warning(self, project):
        rw = duckdb.connect(str(project / "warehouse.duckdb"))
        rw.execute("UPDATE _havn.model_state SET last_run_at = now() - INTERVAL 3 DAY WHERE model_path = 'gold.orders'")
        rw.close()
        c = duckdb.connect(str(project / "warehouse.duckdb"), read_only=True)
        try:
            out = ask("revenue", _ctx(project, FakeProvider(_query({"metrics": ["revenue"]})), conn=c))
        finally:
            c.close()
        assert out["status"] == "answered"
        assert any("gold.orders" in w and "freshness window" in w for w in out["warnings"])
        assert out["chart"]["type"] == "number"

    def test_only_metadata_is_sent(self, project, conn):
        provider = FakeProvider(_query({"metrics": ["revenue"]}))
        out = ask("revenue", _ctx(project, provider, conn=conn))
        sent = provider.sent_text
        assert "revenue" in sent and "Sales region" in sent  # catalog incl. @col docs
        assert SECRET_VALUE not in sent and "north" not in sent
        assert out["data_sent_to_model"] == {"catalog": True, "dimension_values": False, "result_rows": False}

    def test_dimension_values_are_opt_in(self, project, conn):
        provider = FakeProvider(_query({"metrics": ["revenue"], "filters": [
            {"dimension": "country", "op": "=", "value": "NO"}]}))
        ai = AIConfig(provider="openai", model="x", base_url="http://localhost:1/v1", share_dimension_values=True)
        out = ask("revenue in Norway", _ctx(project, provider, ai=ai, conn=conn))
        system = provider.calls[0]["system"]
        assert '"NO"' in system and '"north"' in system
        assert SECRET_VALUE not in provider.sent_text  # not a declared dimension
        assert out["data_sent_to_model"]["dimension_values"] is True
        assert out["result"]["rows"] == [[100.0]]

    def test_invalid_spec_is_retried_with_errors(self, project, conn):
        provider = FakeProvider(
            _query({"metrics": ["revenue"], "dimensions": ["customer"]}),
            _query({"metrics": ["revenue"], "dimensions": ["country"]}),
        )
        out = ask("revenue by customer country", _ctx(project, provider, conn=conn))
        assert out["status"] == "answered" and out["attempts"] == 2
        retry = provider.calls[1]["messages"][-1]["content"]
        assert "customer" in retry and "does not fit the catalog" in retry

    def test_invalid_twice_is_not_guessed(self, project, conn):
        bad = _query({"metrics": ["profit"]})
        provider = FakeProvider(bad, bad)
        out = ask("profit by region", _ctx(project, provider, conn=conn))
        assert out["status"] == "unanswerable"
        assert out["result"] is None and out["sql"] is None
        assert "profit" in out["explanation"]

    def test_unanswerable_with_suggestion(self, project, conn):
        suggestion = {
            "name": "avg_order_value", "description": "Average order amount", "model": "gold.orders",
            "measure": "AVG(amount)", "dimensions": ["region"], "time_dimension": "order_date", "filters": [],
        }
        provider = FakeProvider(_unanswerable(["revenue"], suggestion))
        out = ask("what is the average order value", _ctx(project, provider, conn=conn))
        assert out["status"] == "unanswerable"
        assert out["closest_metrics"][0]["name"] == "revenue"
        sug = out["suggested_metric"]
        assert sug["errors"] == [] and sug["path"] == "metrics/avg_order_value.yml"
        assert "AVG(amount)" in sug["yaml"]

        from havn.engine.ai.service import accept_suggested_metric

        path = accept_suggested_metric(project, sug["definition"], sug["path"])
        assert "avg_order_value" in load_metrics(project)[0]
        with pytest.raises(ValueError):
            accept_suggested_metric(project, sug["definition"], path)

    def test_bad_suggestion_is_flagged(self, project, conn):
        suggestion = {"name": "x", "description": "", "model": "gold.orders", "measure": "SUM(nope)",
                      "dimensions": ["not_a_column"], "time_dimension": None, "filters": []}
        out = ask("q", _ctx(project, FakeProvider(_unanswerable([], suggestion)), conn=conn))
        assert out["suggested_metric"]["yaml"] is None
        assert any("not_a_column" in e for e in out["suggested_metric"]["errors"])

    def test_closest_metrics_fall_back_to_ranking(self, project, conn):
        out = ask("refund rate by region", _ctx(project, FakeProvider(_unanswerable(["made_up"])), conn=conn))
        assert out["closest_metrics"][0]["name"] == "refunds"

    def test_clarify(self, project, conn):
        decision = {"kind": "clarify", "spec": None, "explanation": "", "closest_metrics": ["revenue", "order_count"],
                    "suggested_metric": None, "clarification": "Revenue or order count?"}
        out = ask("how are sales", _ctx(project, FakeProvider(decision), conn=conn))
        assert out["status"] == "clarify" and out["clarification"] == "Revenue or order count?"

    def test_follow_up_sends_previous_spec(self, project, conn):
        first = {"metrics": ["revenue"], "dimensions": ["region"]}
        provider = FakeProvider(_query({**first, "grain": "month"}))
        out = ask("now by month", _ctx(project, provider, conn=conn),
                  history=[{"question": "revenue by region", "spec": first}])
        msgs = provider.calls[0]["messages"]
        assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
        assert json.loads(msgs[1]["content"])["spec"] == first
        assert out["spec"]["grain"] == "month"
        assert out["chart"]["type"] == "line"

    def test_exploratory_is_off_by_default(self, project, conn):
        provider = FakeProvider(_unanswerable())
        out = ask("customers", _ctx(project, provider, conn=conn), exploratory=True)
        assert out["status"] == "unanswerable" and "exploratory" not in out
        assert any("exploratory_sql" in w for w in out["warnings"])
        assert len(provider.calls) == 1

    def test_exploratory_is_unverified_and_read_only(self, project, conn):
        ai = AIConfig(provider="openai", model="x", base_url="http://localhost:1/v1", exploratory_sql=True)
        provider = FakeProvider(_unanswerable(), {"sql": "SELECT COUNT(*) AS n FROM gold.orders", "explanation": "count"})
        out = ask("how many rows", _ctx(project, provider, ai=ai, conn=conn), exploratory=True)
        assert out["status"] == "exploratory" and out["verified"] is False
        assert out["exploratory"]["result"]["rows"] == [[4]]

        provider = FakeProvider(_unanswerable(), {"sql": "DROP TABLE gold.orders", "explanation": ""})
        out = ask("drop", _ctx(project, provider, ai=ai, conn=conn), exploratory=True)
        assert out["status"] == "unanswerable"
        assert "read-only" in out["exploratory"]["error"]

    def test_masking_applies_through_governed_path(self, project):
        from havn.engine.masking import create_policy, ensure_masking_table

        rw = duckdb.connect(str(project / "warehouse.duckdb"))
        ensure_masking_table(rw)
        create_policy(rw, schema_name="gold", table_name="orders", column_name="region",
                      method="redact", exempted_roles=["admin"])
        rw.close()
        c = duckdb.connect(str(project / "warehouse.duckdb"), read_only=True)
        try:
            spec = {"metrics": ["revenue"], "dimensions": ["region"]}
            viewer = ask("revenue by region", _ctx(project, FakeProvider(_query(spec)), role="viewer", conn=c))
            admin = ask("revenue by region", _ctx(project, FakeProvider(_query(spec)), role="admin", conn=c))
        finally:
            c.close()
        assert {r[0] for r in admin["result"]["rows"]} == {"north", "south"}
        assert "north" not in {r[0] for r in viewer["result"]["rows"]}

    def test_provider_error_is_reported(self, project, conn):
        out = ask("q", _ctx(project, FakeProvider(ProviderError("boom")), conn=conn))
        assert out["status"] == "error" and "boom" in out["error"]

    def test_no_metrics(self, tmp_path):
        (tmp_path / "project.yml").write_text("name: empty\n")
        out = ask("revenue?", _ctx(tmp_path, FakeProvider(_unanswerable(explanation=""))))
        assert out["status"] == "unanswerable"
        assert "No metrics" in out["explanation"]


def test_suggest_chart_shapes():
    spec = QuerySpec(metrics=["m"], dimensions=["d", "e", "f"])
    assert suggest_chart(spec, ["d", "e", "f", "m"], [[1, 2, 3, 4]]) == {"type": "table"}
    assert suggest_chart(None, ["x"], [[1]]) == {"type": "table"}
    spec = QuerySpec(metrics=["m"], dimensions=["d"], grain="month")
    chart = suggest_chart(spec, ["month", "d", "m"], [["2026-01", "a", 1], ["2026-01", "b", 2]])
    assert chart == {"type": "line", "x": "month", "y": ["m"], "series": "d"}


# ---------------------------------------------------------------------------
# Eval harness
# ---------------------------------------------------------------------------

EVAL_YML = """cases:
  - question: revenue by region
    expect: {metrics: [revenue], dimensions: [region]}
  - question: average delivery time
    expect: unanswerable
  - question: now by month
    history:
      - question: revenue by region
        spec: {metrics: [revenue], dimensions: [region]}
    expect: {metrics: [revenue], grain: month}
"""


def test_eval_harness(project, conn):
    (project / "tests" / "ask").mkdir(parents=True)
    (project / "tests/ask/sales.yml").write_text(EVAL_YML)
    files = expand_paths(["tests/ask/*.yml"], project)
    cases, errors = load_cases(files)
    assert errors == [] and len(cases) == 3
    provider = FakeProvider(
        _query({"metrics": ["revenue"], "dimensions": ["region"]}),
        _unanswerable(),
        _query({"metrics": ["revenue"], "dimensions": ["region"], "grain": "week"}),
    )
    report = run_eval(cases, _ctx(project, provider, conn=conn))
    assert report["total"] == 3 and report["passed"] == 2
    failed = [r for r in report["results"] if not r["passed"]][0]
    assert failed["mismatches"] == ["grain: expected 'month', got 'week'"]
    assert len(provider.calls[2]["messages"]) == 3  # history sent


def test_eval_compare_normalises():
    case = load_cases.__globals__["EvalCase"](
        name="c", question="q", expect_kind="query",
        expect={"metrics": ["Revenue"], "filters": [{"dimension": "country", "op": "in", "value": ["se", "NO"]}]},
    )
    spec = {"metrics": ["revenue"], "filters": [{"dimension": "country", "op": "in", "value": ["NO", "SE"]}]}
    assert compare(case, "query", spec) == []
    assert compare(case, "unanswerable", None) == ["expected query, got unanswerable"]


def test_eval_load_errors(tmp_path):
    (tmp_path / "bad.yml").write_text("cases:\n  - question: q\n    expect: {colour: red}\n  - nope\n")
    cases, errors = load_cases([tmp_path / "bad.yml"])
    assert cases == [] and len(errors) == 2


# ---------------------------------------------------------------------------
# Providers over real HTTP (a local stub server, no network)
# ---------------------------------------------------------------------------


class _Stub:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                headers = {k.lower(): v for k, v in self.headers.items()}
                stub.requests.append({"path": self.path, "headers": headers, "body": body})
                status, payload = stub.responses.pop(0)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def stub():
    servers = []

    def make(*responses):
        s = _Stub(responses)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


class TestProviders:
    def test_anthropic_request_shape(self, stub, monkeypatch):
        s = stub((200, {"content": [{"type": "text", "text": '{"kind": "query"}'}], "stop_reason": "end_turn"}))
        monkeypatch.setenv("TEST_KEY", "sk-test")
        p = AnthropicProvider(AIConfig(provider="anthropic", model="claude-sonnet-5-5", base_url=s.url, api_key_env="TEST_KEY"))
        out = p.complete_json(system="sys", messages=[{"role": "user", "content": "q"}], schema={"type": "object"})
        assert out == {"kind": "query"}
        req = s.requests[0]
        assert req["path"] == "/v1/messages"
        assert req["headers"]["x-api-key"] == "sk-test"
        assert req["headers"]["anthropic-version"] == "2023-06-01"
        assert req["body"]["model"] == "claude-sonnet-5-5" and req["body"]["system"] == "sys"
        assert req["body"]["output_config"]["format"]["type"] == "json_schema"
        assert "tool_choice" not in req["body"]

    def test_anthropic_falls_back_without_structured_output(self, stub, monkeypatch):
        s = stub(
            (400, {"type": "error", "error": {"message": "output_config.format: unsupported"}}),
            (200, {"content": [{"type": "text", "text": 'Sure:\n```json\n{"kind": "clarify"}\n```'}]}),
        )
        monkeypatch.setenv("TEST_KEY", "k")
        p = AnthropicProvider(AIConfig(provider="anthropic", model="m", base_url=s.url, api_key_env="TEST_KEY"))
        assert p.complete_json(system="s", messages=[{"role": "user", "content": "q"}], schema={})["kind"] == "clarify"
        assert "output_config" not in s.requests[1]["body"]

    def test_anthropic_refusal_and_errors(self, stub, monkeypatch):
        s = stub((200, {"content": [], "stop_reason": "refusal"}), (500, {"error": "x"}))
        monkeypatch.setenv("TEST_KEY", "k")
        p = AnthropicProvider(AIConfig(provider="anthropic", model="m", base_url=s.url, api_key_env="TEST_KEY"))
        with pytest.raises(ProviderError, match="declined"):
            p.complete_json(system="s", messages=[], schema=None)
        with pytest.raises(ProviderError, match="HTTP 500"):
            p.complete_json(system="s", messages=[], schema=None)

    def test_anthropic_needs_key(self, monkeypatch):
        monkeypatch.delenv("NO_SUCH_KEY", raising=False)
        with pytest.raises(AIConfigError, match="NO_SUCH_KEY"):
            AnthropicProvider(AIConfig(provider="anthropic", api_key_env="NO_SUCH_KEY"))

    def test_openai_compatible_local(self, stub):
        s = stub((200, {"choices": [{"message": {"content": '{"kind": "unanswerable"}'}}]}))
        cfg = AIConfig(provider="openai", model="qwen2.5", base_url=s.url + "/v1", api_key_env="UNSET_KEY_X")
        assert cfg.is_local
        p = OpenAICompatibleProvider(cfg)
        assert p.complete_json(system="s", messages=[{"role": "user", "content": "q"}])["kind"] == "unanswerable"
        req = s.requests[0]
        assert req["path"] == "/v1/chat/completions"
        assert "authorization" not in req["headers"]
        assert req["body"]["messages"][0] == {"role": "system", "content": "s"}
        assert req["body"]["response_format"] == {"type": "json_object"}

    def test_openai_remote_needs_key(self, monkeypatch):
        monkeypatch.delenv("UNSET_KEY_X", raising=False)
        with pytest.raises(AIConfigError):
            OpenAICompatibleProvider(AIConfig(provider="openai", model="m", base_url="https://api.example.com/v1",
                                              api_key_env="UNSET_KEY_X"))

    def test_unreachable(self):
        p = OpenAICompatibleProvider(AIConfig(provider="openai", model="m", base_url="http://127.0.0.1:9/v1", timeout=2))
        with pytest.raises(ProviderError, match="could not reach"):
            p.complete_json(system="s", messages=[])

    def test_extract_json(self):
        assert extract_json_object('text {"a": {"b": "}"}} trailing') == {"a": {"b": "}"}}
        with pytest.raises(ProviderError):
            extract_json_object("no json here")


class TestConfig:
    def test_defaults(self):
        cfg = parse_ai_config(None)
        assert cfg.provider == "anthropic" and cfg.model == "claude-sonnet-5-5"
        assert cfg.api_key_env == "ANTHROPIC_API_KEY"
        assert not cfg.exploratory_sql and not cfg.share_dimension_values and not cfg.summarize_results

    def test_ollama_alias_and_validation(self):
        cfg = parse_ai_config({"provider": "ollama", "model": "llama3", "base_url": "http://localhost:11434/v1/"})
        assert cfg.provider == "openai" and cfg.base_url == "http://localhost:11434/v1" and cfg.is_local
        with pytest.raises(AIConfigError):
            parse_ai_config({"provider": "skynet"})
        with pytest.raises(AIConfigError):
            parse_ai_config({"base_url": "ftp://x"})

    def test_public_dict_hides_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-very-secret")
        d = parse_ai_config({}).public_dict()
        assert d["api_key_set"] is True and "sk-very-secret" not in json.dumps(d)


# ---------------------------------------------------------------------------
# API, CLI and MCP surfaces
# ---------------------------------------------------------------------------


@pytest.fixture
def fake(monkeypatch):
    holder = {"provider": None}

    def factory(_config):
        return holder["provider"]

    monkeypatch.setattr("havn.engine.ai.providers.provider_from_config", factory)
    return holder


@pytest.fixture
def client(project):
    import havn.server.app as server_app
    from havn.server.deps import invalidate_config_cache, reset_shared_conn

    reset_shared_conn()
    server_app.PROJECT_DIR = project
    server_app.AUTH_ENABLED = False
    invalidate_config_cache()
    return TestClient(server_app.app)


class TestAPI:
    def test_status(self, client):
        data = client.get("/api/ask/status").json()
        assert data["configured"] is True and data["provider"] == "openai" and data["is_local"] is True
        assert data["metrics"] == 3

    def test_ask(self, client, fake):
        fake["provider"] = FakeProvider(_query({"metrics": ["order_count"], "dimensions": ["country"],
                                                "order_by": [{"field": "order_count", "direction": "desc"}],
                                                "limit": 1}))
        resp = client.post("/api/ask", json={"question": "which country has most orders"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "answered"
        assert data["result"]["rows"] == [["NO", 2]]

    def test_ask_history_and_validation(self, client, fake):
        fake["provider"] = FakeProvider(_query({"metrics": ["revenue"], "grain": "month"}))
        resp = client.post("/api/ask", json={"question": "monthly", "history": [
            {"question": "revenue", "spec": {"metrics": ["revenue"]}}]})
        assert resp.json()["spec"]["grain"] == "month"
        assert client.post("/api/ask", json={"question": ""}).status_code == 422

    def test_accept_metric(self, client, project):
        definition = {"name": "avg_amount", "model": "gold.orders", "measure": "AVG(amount)", "dimensions": ["region"]}
        resp = client.post("/api/ask/accept-metric", json={"definition": definition})
        assert resp.status_code == 200 and resp.json()["path"] == "metrics/avg_amount.yml"
        assert client.post("/api/ask/accept-metric", json={"definition": definition}).status_code == 400
        bad = {"name": "x", "model": "gold.orders", "measure": "SUM(a); DROP TABLE t"}
        assert client.post("/api/ask/accept-metric", json={"definition": bad}).status_code == 400
        resp = client.post("/api/ask/accept-metric", json={"definition": {**definition, "name": "y"}, "path": "../y.yml"})
        assert resp.status_code == 400

    def test_eval_endpoint(self, client, fake, project):
        (project / "tests" / "ask").mkdir(parents=True)
        (project / "tests/ask/a.yml").write_text("cases:\n  - question: revenue\n    expect: {metrics: [revenue]}\n")
        fake["provider"] = FakeProvider(_query({"metrics": ["revenue"]}))
        data = client.post("/api/ask/eval", json={}).json()
        assert data["passed"] == 1 and data["files"] == ["tests/ask/a.yml"]
        assert client.post("/api/ask/eval", json={"paths": ["../*.yml"]}).status_code == 400

    def test_viewer_can_ask_but_not_accept(self, project, fake):
        import havn.server.app as server_app
        import havn.server.deps as deps

        deps.reset_shared_conn()
        deps._clear_config_cache()
        deps.invalidate_token_cache()
        server_app.PROJECT_DIR = project
        server_app.AUTH_ENABLED = True
        try:
            c = TestClient(server_app.app)
            admin = c.post("/api/auth/setup", json={"username": "admin", "password": "adminpass1",
                                                     "role": "admin"}).json()["token"]
            r = c.post("/api/users", json={"username": "vic", "password": "vicpass12", "role": "viewer"},
                       headers={"Authorization": f"Bearer {admin}"})
            assert r.status_code == 200, r.text
            token = c.post("/api/auth/login", json={"username": "vic", "password": "vicpass12"}).json()["token"]
            h = {"Authorization": f"Bearer {token}"}
            fake["provider"] = FakeProvider(_query({"metrics": ["revenue"]}))
            assert c.post("/api/ask", json={"question": "revenue"}, headers=h).status_code == 200
            definition = {"name": "z", "model": "gold.orders", "measure": "COUNT(*)"}
            assert c.post("/api/ask/accept-metric", json={"definition": definition}, headers=h).status_code == 403
        finally:
            server_app.AUTH_ENABLED = False


class TestCLI:
    def test_ask_and_continue(self, project, fake):
        from havn.cli import app

        runner = CliRunner()
        fake["provider"] = FakeProvider(_query({"metrics": ["revenue"], "dimensions": ["region"]}, "Revenue by region"))
        result = runner.invoke(app, ["ask", "revenue", "by", "region", "--project", str(project)])
        assert result.exit_code == 0, result.output
        assert "north" in result.output and "lineage" in result.output and "SELECT" in result.output

        provider = FakeProvider(_query({"metrics": ["revenue"], "dimensions": ["region"], "grain": "month"}))
        fake["provider"] = provider
        result = runner.invoke(app, ["ask", "now by month", "-c", "--json", "--project", str(project)])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["spec"]["grain"] == "month"
        assert provider.calls[0]["messages"][0]["content"] == "revenue by region"

    def test_save_suggestion(self, project, fake):
        from havn.cli import app

        suggestion = {"name": "max_order", "description": "", "model": "gold.orders", "measure": "MAX(amount)",
                      "dimensions": [], "time_dimension": None, "filters": []}
        fake["provider"] = FakeProvider(_unanswerable(["revenue"], suggestion))
        result = CliRunner().invoke(app, ["ask", "largest order", "--save-suggestion", "--project", str(project)])
        assert result.exit_code == 0, result.output
        assert "No defined metric answers this" in result.output
        assert (project / "metrics/max_order.yml").exists()

    def test_eval(self, project, fake):
        from havn.cli import app

        (project / "tests" / "ask").mkdir(parents=True)
        (project / "tests/ask/a.yml").write_text(EVAL_YML)
        fake["provider"] = FakeProvider(
            _query({"metrics": ["revenue"], "dimensions": ["region"]}),
            _unanswerable(),
            _query({"metrics": ["revenue"], "dimensions": ["region"], "grain": "month"}),
        )
        result = CliRunner().invoke(app, ["ask", "--eval", "--project", str(project)])
        assert result.exit_code == 0, result.output
        assert "3/3 passed" in result.output

        fake["provider"] = FakeProvider(_unanswerable(), _unanswerable(), _unanswerable())
        result = CliRunner().invoke(app, ["ask", "--eval", "tests/ask/*.yml", "--project", str(project)])
        assert result.exit_code == 1


def test_mcp_ask_tool(project, fake):
    from havn.mcp.server import MCPServer

    fake["provider"] = FakeProvider(_query({"metrics": ["revenue"]}))
    server = MCPServer(project)
    resp = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "ask", "arguments": {"question": "total revenue"}}})
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["status"] == "answered" and payload["result"]["rows"] == [[180.0]]
