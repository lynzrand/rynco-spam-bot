"""Small synchronous clients for Telegram and vision Chat Completions."""

import base64
import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path


class APIError(Exception):
    """Safe-to-log error: never includes request URLs, tokens, or response bodies."""

    def __init__(self, service, code=0, retry_after=5, description=""):
        super().__init__(f"{service} request failed (status {code})")
        self.code = code
        self.retry_after = retry_after
        self.description = description


def request_json(url, payload, headers=None, timeout=45, service="API"):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.load(response)
        if not isinstance(result, dict):
            raise ValueError("Expected a JSON object")
        return result
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read(65536))
        except (ValueError, OSError):
            error = {}
        if not isinstance(error, dict):
            error = {}
        raise APIError(
            service,
            exc.code,
            error.get("parameters", {}).get("retry_after", 5),
            error.get("description", ""),
        ) from None
    except (OSError, ValueError):
        raise APIError(service) from None


class Telegram:
    def __init__(self, token):
        self.root = f"https://api.telegram.org/bot{token}/"
        self.file_root = f"https://api.telegram.org/file/bot{token}/"

    def call(self, method, **params):
        result = request_json(self.root + method, params, service="Telegram")
        if not result.get("ok"):
            raise APIError(
                "Telegram",
                result.get("error_code", 0),
                result.get("parameters", {}).get("retry_after", 5),
                result.get("description", ""),
            )
        return result["result"]

    def download(self, file_id, limit=20 * 1024 * 1024):
        """Download locally; never expose the token-bearing file URL to the model."""
        file = self.call("getFile", file_id=file_id)
        if file.get("file_size", 0) > limit:
            raise APIError("Media exceeds download limit")
        try:
            with urllib.request.urlopen(
                self.file_root + file["file_path"], timeout=30
            ) as response:
                data = response.read(limit + 1)
        except OSError:
            raise APIError("Telegram media download") from None
        if len(data) > limit:
            raise APIError("Media exceeds download limit")
        return data

    def image(self, file_id):
        return image_data(self.download(file_id, 8 * 1024 * 1024))


def image_data(data):
    if data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        mime = "image/webp"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    else:
        raise APIError("Unsupported image")
    return f"data:{mime};base64," + base64.b64encode(data).decode()


class Classifier:
    def __init__(self, base_url, key, model, context):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.key, self.model, self.context = key, model, context
        self.prompt = Path(__file__).with_name("system_prompt.txt").read_text()

    def classify(self, evidence, images):
        content = [{"type": "text", "text": json.dumps(evidence, ensure_ascii=False)}]
        for label, data in images:
            content.extend(
                [
                    {"type": "text", "text": label},
                    {"type": "image_url", "image_url": {"url": data}},
                ]
            )
        result = request_json(
            self.url,
            {
                "model": self.model,
                "reasoning_effort": "low",
                "messages": [
                    {"role": "system", "content": self.prompt},
                    {"role": "system", "content": "Group context: " + self.context},
                    {"role": "user", "content": content},
                ],
                "response_format": {"type": "json_object"},
            },
            {
                "Authorization": "Bearer " + self.key,
                "User-Agent": "rynco-spam-bot/0.1",
                "x-opencode-session": str(uuid.uuid4()),
            },
            service="Classifier",
        )
        try:
            choice = result["choices"][0]
            if not isinstance(choice, dict) or choice.get("finish_reason") != "stop":
                raise ValueError("Incomplete completion")
            verdict = json.loads(choice["message"]["content"])
            if (
                not isinstance(verdict, dict)
                or verdict.get("verdict") not in {"clean", "suspicious", "spam"}
                or not isinstance(verdict.get("reason"), str)
                or not 1 <= len(verdict["reason"]) <= 300
            ):
                raise ValueError("Invalid classification")
            return {
                "verdict": verdict["verdict"],
                "reason": verdict["reason"],
                # Only the exact marker for an inspection gap relaxes a verdict, so an
                # unexpected value can never fail the response (that would review spam).
                "basis": (
                    "uninspectable"
                    if verdict.get("basis") == "uninspectable"
                    else "visible"
                ),
            }
        except (KeyError, IndexError, TypeError, ValueError):
            raise APIError("Invalid classifier response") from None
