"""Run on the RunPod GPU pod. Serves SPECTER2 embeddings over HTTP so the ingestion
pipeline (wherever it runs - e.g. the AWS box) can offload the GPU-heavy embedding
step here instead of loading the model in-process. See
litgraph.ingest.embeddings.embed_texts (the client side) and
litgraph.config.Settings.embedding_service_url.

Reached via RunPod's HTTP service proxy (Connect tab -> "HTTP services"), which
terminates TLS at RunPod's edge - not Tailscale. RunPod's proxy connects to the
container's real network interface, so this must listen on 0.0.0.0, not loopback.

Set EMBEDDING_SERVICE_TOKEN to the same random secret on both this pod and wherever
the pipeline runs - without it, this endpoint has zero access control and anyone with
the proxied URL can burn your GPU time.

    export EMBEDDING_SERVICE_TOKEN=<same value as the client's .env>
    python scripts/runpod_embedding_server.py &

Then set EMBEDDING_SERVICE_URL=https://<pod-id>-8000.proxy.runpod.net in the .env
wherever the pipeline runs.
"""

import hmac
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from litgraph.config import get_settings
from litgraph.ingest.embeddings import embed_texts


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/embed":
            self.send_error(404)
            return
        token = get_settings().embedding_service_token
        if token and not hmac.compare_digest(
            self.headers.get("Authorization", ""), f"Bearer {token}"
        ):
            self.send_error(401)
            return
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        vectors = embed_texts(body["texts"])
        payload = json.dumps({"vectors": vectors}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        print(f"[embedding-server] {self.address_string()} - {format % args}")


def main(port: int = 8000) -> None:
    server = HTTPServer(("0.0.0.0", port), _Handler)
    print(f"SPECTER2 embedding server listening on 0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
