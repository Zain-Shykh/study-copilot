"""Phase 0 verification: starts a placeholder FastAPI route, tunnels it
through ngrok's static domain, and confirms the public URL actually
reaches it. Disposable — not imported by agent/.
"""

import os
import subprocess
import sys
import threading
import time

import dotenv
import httpx
import uvicorn
from fastapi import FastAPI

LOCAL_PORT = 8000


def create_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


def start_placeholder_server(port: int) -> threading.Thread:
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return thread


def start_ngrok_tunnel(domain: str, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        ["ngrok", "http", "--domain", domain, str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_local_server(port: int, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise TimeoutError("placeholder FastAPI server did not start locally")


def main() -> None:
    dotenv.load_dotenv()

    domain = os.environ.get("NGROK_STATIC_DOMAIN", "")
    if not domain:
        print("Missing required env var: NGROK_STATIC_DOMAIN", file=sys.stderr)
        sys.exit(1)

    start_placeholder_server(LOCAL_PORT)

    try:
        wait_for_local_server(LOCAL_PORT)
    except TimeoutError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    try:
        ngrok_process = start_ngrok_tunnel(domain, LOCAL_PORT)
    except FileNotFoundError:
        print(
            "ngrok binary not found — install it and run 'ngrok config add-authtoken' "
            "first (see specs/phase-0-environment-setup.md §1.3)",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        time.sleep(3)
        try:
            response = httpx.get(f"https://{domain}/health", timeout=10)
        except httpx.HTTPError as e:
            print(f"ngrok tunnel check failed: {e}", file=sys.stderr)
            sys.exit(1)

        if response.status_code != 200 or response.json() != {"status": "ok"}:
            print(
                f"ngrok tunnel check failed: got {response.status_code} {response.text}",
                file=sys.stderr,
            )
            sys.exit(1)

        print(f"ngrok tunnel OK: {domain}/health -> 200")
    finally:
        ngrok_process.terminate()
        ngrok_process.wait(timeout=5)


if __name__ == "__main__":
    main()
