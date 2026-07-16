"""health service — liveness only. Fully implemented; no auth, no deps."""
from conpass_common import create_app, lambda_handler

app = create_app(service="health")  # provides GET /health

handler = lambda_handler(app)
