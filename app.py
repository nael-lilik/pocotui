from flask import Flask, render_template_string
import subprocess, os
app = Flask(__name__)
PORT = 7890

def sh(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
        return r.stdout + r.stderr
    except Exception as e:
        return str(e)

@app.route("/")
def index():
    return render_template_string(open("/root/pocotui/dashboard.html").read())

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
