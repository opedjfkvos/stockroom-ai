"""The narrow part of the Ollama API used by the desktop setup wizard."""

import json
from urllib.request import Request, urlopen

BASE_URL = "http://127.0.0.1:11434"


def installed_models(endpoint=BASE_URL):
    with urlopen(endpoint + "/api/tags", timeout=2) as response:
        payload = json.load(response)
    return {entry["name"] for entry in payload["models"]}


def pull_model(model, report, endpoint=BASE_URL):
    request = Request(endpoint + "/api/pull", data=json.dumps({"model": model, "stream": True}).encode(),
                      headers={"Content-Type": "application/json"})
    complete = False
    with urlopen(request, timeout=90) as response:
        for line in response:
            event = json.loads(line)
            if event.get("error"):
                raise ValueError(event["error"])
            if event.get("status") == "success":
                complete = True
            elif event.get("total"):
                percent = min(100, 100 * event.get("completed", 0) // event["total"])
                report(percent)
    if not complete or model not in installed_models(endpoint):
        raise ValueError("Ollama did not confirm the model is ready.")

