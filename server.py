"""
سيرفر وهمي (Flask) لا يقوم بأي شيء فعلي سوى الرد على نبضات الصحة (health checks).
سبب وجوده: استضافة Render المجانية من نوع "Web Service" تتطلب أن تستمع الخدمة على
منفذ HTTP، وإلا تعتبرها معطلة وتوقفها. يعمل هذا السيرفر بالتوازي مع البوت في Thread منفصل.
"""

import logging
from flask import Flask

from config import PORT

logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = Flask(__name__)


@app.route("/")
def health_check():
    return "VONE bot is alive ✅", 200


def run_server():
    app.run(host="0.0.0.0", port=PORT)
