import logging

from flask import Flask, render_template

from web.auth import require_auth
from web.data import get_dashboard_data

logging.basicConfig(level="INFO")
app = Flask(__name__)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}, 200


@app.get("/")
@require_auth
def dashboard():
    return render_template("dashboard.html", **get_dashboard_data())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
