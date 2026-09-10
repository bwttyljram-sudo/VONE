import os
import logging
from flask import Flask

logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__)


@app.route("/")
def health_check():
    return "VONE bot is alive ✅", 200


def run_server():
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
