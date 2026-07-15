import logging

from flask import Flask, render_template, request

from web.auth import require_auth
from web.data import TIER_BROKERS, get_dashboard_data, mark_activity

logging.basicConfig(level="INFO")
app = Flask(__name__)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}, 200


@app.get("/")
@require_auth
def dashboard():
    mark_activity()
    tier = request.args.get("tier", "conservative")
    if tier not in TIER_BROKERS:
        tier = "conservative"
    return render_template("dashboard.html", **get_dashboard_data(tier))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
