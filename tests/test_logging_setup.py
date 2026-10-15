"""Tests for agent/logging_setup.py and the logging-related config validation.
See specs/logging-observability.md §10."""

import asyncio
import inspect
import json
import logging
import threading
from typing import TypedDict

import httpx
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from agent import config as config_module
from agent.graph.nodes import whatsapp_send
from agent.logging_setup import (
    _HANDLER_MARK,
    log_node,
    request_id_var,
    setup_logging,
)


@pytest.fixture(autouse=True)
def restore_root_logger():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    quiet = {n: logging.getLogger(n).level for n in ("httpx", "urllib3", "asyncio")}
    yield
    for h in list(root.handlers):
        if h not in handlers:
            root.removeHandler(h)
            h.close()
    root.setLevel(level)
    for n, lv in quiet.items():
        logging.getLogger(n).setLevel(lv)


def _our_handlers():
    return [h for h in logging.getLogger().handlers if getattr(h, _HANDLER_MARK, False)]


def _flush():
    for h in _our_handlers():
        h.flush()


class TestSetupLogging:
    def test_creates_file_and_is_idempotent(self, tmp_path):
        setup_logging("INFO", "text", tmp_path)
        setup_logging("INFO", "text", tmp_path)

        assert len(_our_handlers()) == 2
        logging.getLogger("t").info("hello")
        _flush()
        assert "hello" in (tmp_path / "app.log").read_text()

    def test_unwritable_dir_falls_back_to_console_only(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x")

        setup_logging("INFO", "text", blocker / "sub")  # must not raise

        assert len(_our_handlers()) == 1

    def test_third_party_loggers_forced_to_warning(self, tmp_path):
        logging.getLogger("httpx").setLevel(logging.DEBUG)

        setup_logging("INFO", "text", tmp_path)

        assert logging.getLogger("httpx").level == logging.WARNING

    def test_text_line_has_request_id_and_function(self, tmp_path):
        setup_logging("INFO", "text", tmp_path)
        token = request_id_var.set("abc12345")
        try:
            logging.getLogger("t").info("something happened")
        finally:
            request_id_var.reset(token)
        _flush()

        line = (tmp_path / "app.log").read_text().strip()
        assert "abc12345" in line
        assert "test_text_line_has_request_id_and_function" in line
        assert "something happened" in line

    def test_json_line_parses_and_exc_only_with_exc_info(self, tmp_path):
        setup_logging("INFO", "json", tmp_path)
        token = request_id_var.set("abc12345")
        try:
            logging.getLogger("t").info("plain")
            try:
                raise ValueError("boom")
            except ValueError:
                logging.getLogger("t").exception("failed")
        finally:
            request_id_var.reset(token)
        _flush()

        plain, failed = [json.loads(l) for l in (tmp_path / "app.log").read_text().splitlines()]
        for entry in (plain, failed):
            assert {"ts", "level", "request_id", "logger", "function", "line", "message"} <= entry.keys()
            assert entry["request_id"] == "abc12345"
        assert "exc" not in plain
        assert "ValueError: boom" in failed["exc"]

    def test_level_filters_info(self, tmp_path):
        setup_logging("WARNING", "text", tmp_path)

        logging.getLogger("t").info("quiet")
        logging.getLogger("t").warning("loud")
        _flush()

        text = (tmp_path / "app.log").read_text()
        assert "quiet" not in text and "loud" in text


class TestConfigValidation:
    @pytest.fixture(autouse=True)
    def env(self, monkeypatch):
        monkeypatch.setattr(config_module.dotenv, "load_dotenv", lambda: None)
        for name in config_module._REQUIRED_ENV_VARS:
            monkeypatch.setenv(name, "x")
        for name in ("LOG_LEVEL", "LOG_FORMAT", "LOG_DIR"):
            monkeypatch.delenv(name, raising=False)

    def test_defaults(self):
        s = config_module.load_settings()
        assert (s.log_level, s.log_format, s.log_dir) == ("INFO", "text", "logs")

    def test_values_are_normalised(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "debug")
        monkeypatch.setenv("LOG_FORMAT", "JSON")
        s = config_module.load_settings()
        assert (s.log_level, s.log_format) == ("DEBUG", "json")

    def test_invalid_level_raises(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "LOUD")
        with pytest.raises(RuntimeError, match="Invalid LOG_LEVEL"):
            config_module.load_settings()

    def test_invalid_format_raises(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "xml")
        with pytest.raises(RuntimeError, match="Invalid LOG_FORMAT"):
            config_module.load_settings()


class TestLogNode:
    def test_sync_success(self, caplog):
        caplog.set_level(logging.INFO)

        @log_node
        def node(state):
            return {"a": 1}

        assert node({}) == {"a": 1}
        assert "node=node ok in" in caplog.text

    def test_async_success(self, caplog):
        caplog.set_level(logging.INFO)

        @log_node
        async def node(state):
            return {"a": 1}

        assert asyncio.run(node({})) == {"a": 1}
        assert "node=node ok in" in caplog.text

    def test_failure_logs_traceback_and_reraises(self, caplog):
        @log_node
        def node(state):
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            node({})

        record = next(r for r in caplog.records if r.levelno == logging.ERROR)
        assert "node=node failed" in record.getMessage()
        assert record.exc_info is not None

    def test_interrupt_is_a_pause_not_an_error(self, caplog):
        caplog.set_level(logging.INFO)

        @log_node
        def node(state):
            raise GraphInterrupt()

        with pytest.raises(GraphInterrupt):
            node({})

        assert "node=node paused" in caplog.text
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_signature_and_asyncness_preserved(self):
        def sync_node(state, config: RunnableConfig):
            return {}

        async def async_node(state, config: RunnableConfig):
            return {}

        assert not inspect.iscoroutinefunction(log_node(sync_node))
        assert inspect.iscoroutinefunction(log_node(async_node))
        assert list(inspect.signature(log_node(sync_node)).parameters) == ["state", "config"]


class _State(TypedDict, total=False):
    answer: str
    done: bool


class TestLogNodeInRealGraph:
    def test_wrapped_nodes_run_interrupt_and_propagate_request_id(self, caplog):
        caplog.set_level(logging.INFO)
        seen = {}

        @log_node
        def ask(state: _State, config: RunnableConfig) -> dict:
            seen["request_id"] = request_id_var.get()
            seen["thread"] = threading.current_thread()
            seen["thread_id"] = config["configurable"]["thread_id"]
            return {"answer": interrupt("question?")}

        @log_node
        async def finish(state: _State, config: RunnableConfig) -> dict:
            return {"done": True}

        g = StateGraph(_State)
        g.add_node("ask", ask)
        g.add_node("finish", finish)
        g.add_edge(START, "ask")
        g.add_edge("ask", "finish")
        g.add_edge("finish", END)
        graph = g.compile(checkpointer=InMemorySaver())
        cfg = {"configurable": {"thread_id": "t1"}}

        async def run():
            request_id_var.set("req12345")
            paused = await graph.ainvoke({}, config=cfg)
            assert "__interrupt__" in paused
            return await graph.ainvoke(Command(resume="yes"), config=cfg)

        result = asyncio.run(run())

        assert result["answer"] == "yes" and result["done"] is True
        assert seen["thread_id"] == "t1"  # LangGraph still injected config through the wrapper
        assert seen["thread"] is not threading.main_thread()  # sync node ran in a worker thread...
        assert seen["request_id"] == "req12345"  # ...and still saw the request id
        assert "node=ask paused" in caplog.text
        assert "node=finish ok in" in caplog.text
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_request_id_reaches_create_task(self):
        async def run():
            request_id_var.set("task1234")

            async def child():
                return request_id_var.get()

            return await asyncio.create_task(child())

        assert asyncio.run(run()) == "task1234"


class TestWhatsappErrorBodyLogged:
    def test_meta_error_body_logged_and_error_still_raised(self, monkeypatch, caplog):
        real_client = httpx.AsyncClient

        def handler(request):
            return httpx.Response(400, json={"error": {"message": "token expired", "code": 190}})

        monkeypatch.setattr(
            whatsapp_send.httpx,
            "AsyncClient",
            lambda **kw: real_client(transport=httpx.MockTransport(handler)),
        )

        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(whatsapp_send.send_whatsapp_message("tok", "phone", "923001234567", "hi"))

        errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
        assert any("status=400" in m and "token expired" in m for m in errors)
        assert not any("Bearer" in r.getMessage() for r in caplog.records)  # token never logged
