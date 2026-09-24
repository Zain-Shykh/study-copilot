"""FastAPI app entrypoint — mounts the webhook route and starts APScheduler."""

import asyncio
import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from google import genai
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import ConnectionPool

from agent.config import load_settings
from agent.graph.assignment_graph import build_assignment_graph
from agent.graph.email_graph import build_email_graph
from agent.graph.nodes.whatsapp_send import send_whatsapp_message
from agent.graph.recovery import scan_for_interrupted_threads
from agent.graph.router_graph import build_router_graph
from agent.scheduler.jobs import poll_classroom_job, poll_gmail_job
from agent.webhook.routes import router as webhook_router

logger = logging.getLogger(__name__)

_POLL_HOURS = "8,20"  # 08:00/20:00 local time, hardcoded


def create_app() -> FastAPI:
    """Loads Settings (fails fast if misconfigured), then wires up the
    Postgres pool, LangGraph checkpointer, Gemini client, and compiled
    router + assignment graphs for the webhook route to use. Also sets up
    the background-task registry that keeps in-flight assignment runs alive
    (asyncio.create_task results must be referenced somewhere or they can
    be garbage-collected mid-run). Also starts an AsyncIOScheduler running
    the twice-daily Gmail/Classroom proactive-poll jobs.
    """
    settings = load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = ConnectionPool(settings.database_url, open=True)
        async with AsyncPostgresSaver.from_conn_string(settings.database_url) as checkpointer:
            await checkpointer.setup()

            app.state.settings = settings
            app.state.pool = pool
            app.state.genai_client = genai.Client(api_key=settings.gemini_api_key)
            app.state.graph = build_router_graph(checkpointer)
            app.state.assignment_graph = build_assignment_graph(checkpointer)
            app.state.email_graph = build_email_graph(checkpointer)
            app.state.background_tasks: set[asyncio.Task] = set()

            with pool.connection() as conn:
                interrupted = await scan_for_interrupted_threads(
                    app.state.assignment_graph, app.state.email_graph, conn
                )
            for name in interrupted:
                try:
                    await send_whatsapp_message(
                        settings.meta_whatsapp_access_token,
                        settings.meta_whatsapp_phone_number_id,
                        settings.my_whatsapp_number,
                        f'I was working on "{name}" when I restarted — send it '
                        "again if you'd like me to retry.",
                    )
                except Exception:
                    logger.exception("Failed to send crash-recovery heads-up for %s", name)

            scheduler = AsyncIOScheduler()
            scheduler.add_job(
                poll_gmail_job,
                CronTrigger(hour=_POLL_HOURS),
                args=[
                    pool,
                    app.state.genai_client,
                    settings.gemini_model,
                    settings.meta_whatsapp_access_token,
                    settings.meta_whatsapp_phone_number_id,
                    settings.my_whatsapp_number,
                ],
            )
            scheduler.add_job(
                poll_classroom_job,
                CronTrigger(hour=_POLL_HOURS),
                args=[
                    pool,
                    settings.meta_whatsapp_access_token,
                    settings.meta_whatsapp_phone_number_id,
                    settings.my_whatsapp_number,
                ],
            )
            scheduler.start()
            app.state.scheduler = scheduler

            yield

            scheduler.shutdown(wait=False)

        pool.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(webhook_router)
    return app
