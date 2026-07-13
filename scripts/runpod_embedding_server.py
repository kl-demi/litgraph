"""Run on the RunPod GPU pod. Serves SPECTER2 embeddings over HTTP so the ingestion
pipeline (wherever it runs - e.g. the AWS box) can offload the GPU-heavy embedding
step here instead of loading the model in-process. See
litgraph.ingest.embeddings.embed_texts (the client side) and
litgraph.config.Settings.embedding_service_url.

Expose this to the tailnet with `tailscale serve`, not a raw bind - RunPod pods run
Tailscale in userspace-networking mode (no /dev/net/tun), so inbound tailnet
connections only reach this process if tailscaled is told to forward them:

    python scripts/runpod_embedding_server.py &
    tailscale serve --http=8000 http://127.0.0.1:8000

Then set EMBEDDING_SERVICE_URL=http://<pod-tailscale-ip-or-magicdns-name>:8000 in the
.env wherever the pipeline runs.
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from litgraph.ingest.embeddings import embed_texts


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        if self.path != "/embed":
            self.send_error(404)
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
    server = HTTPServer(("127.0.0.1", port), _Handler)
    print(f"SPECTER2 embedding server listening on 127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
