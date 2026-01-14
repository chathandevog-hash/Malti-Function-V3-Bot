import os
from flask import Flask

app = Flask(__name__)

@app.get("/")
def home():
    return "✅ URL Uploader Bot Running"

@app.get("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
