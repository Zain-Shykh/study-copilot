"""FastAPI app entrypoint — mounts the webhook route and starts APScheduler."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from google import genai
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool

from agent.config import load_settings
from agent.graph.router_graph import build_router_graph
from agent.webhook.routes import router as webhook_router


def create_app() -> FastAPI:
    """Loads Settings (fails fast if misconfigured), then wires up the
    Postgres pool, LangGraph checkpointer, Gemini client, and compiled
    router graph for the webhook route to use. APScheduler is not started
    in this phase (Phase 4's job).
    """
    settings = load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = ConnectionPool(settings.database_url, open=True)
        with PostgresSaver.from_conn_string(settings.database_url) as checkpointer:
            checkpointer.setup()

            app.state.settings = settings
            app.state.pool = pool
            app.state.genai_client = genai.Client(api_key=settings.gemini_api_key)
            app.state.graph = build_router_graph(checkpointer)

            yield

        pool.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(webhook_router)
    return app
