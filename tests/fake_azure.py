"""An httpx transport that answers Azure OpenAI chat and embedding calls offline, for the tests.

Chat calls get a schema-valid structured answer (Instructor's tool call or JSON content), filled with
simple placeholder values; embedding calls get small vectors. Nothing leaves the machine.
"""

import json

import httpx


def _instance(schema: dict, defs: dict):
    if "$ref" in schema:
        return _instance(defs[schema["$ref"].split("/")[-1]], defs)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            options = [s for s in schema[key] if s.get("type") != "null"] or schema[key]
            return _instance(options[0], defs)
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object" or (kind is None and "properties" in schema):
        return {name: _instance(sub, defs) for name, sub in (schema.get("properties") or {}).items()}
    if kind == "array" or (kind is None and "items" in schema):
        return [_instance(schema.get("items") or {"type": "string"}, defs)]
    return {"string": "The answer states a figure.", "integer": 1, "number": 1.0, "boolean": True}.get(kind, "x")


def _schema_in_messages(messages: list) -> dict:
    """Instructor's JSON mode puts the response schema in the system message, after "json_schema:"."""
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and "json_schema:" in content:
            text = content.split("json_schema:", 1)[1]
            start = text.find("{")
            try:
                schema, _ = json.JSONDecoder().raw_decode(text[start:])
                return schema
            except ValueError:
                continue
    return {}


class FakeAzure:
    def __init__(self):
        self.chat_calls = 0
        self.embedding_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if request.url.path.endswith("/embeddings"):
            self.embedding_calls += 1
            inputs = body.get("input")
            inputs = inputs if isinstance(inputs, list) else [inputs]
            return httpx.Response(200, json={
                "object": "list", "model": "fake-embedding",
                "data": [{"object": "embedding", "index": i, "embedding": [0.1, 0.2, 0.3]} for i in range(len(inputs))],
                "usage": {"prompt_tokens": 5, "total_tokens": 5}})
        self.chat_calls += 1
        message = {"role": "assistant", "content": None}
        if body.get("tools"):
            function = body["tools"][0]["function"]
            schema = function.get("parameters") or {}
            arguments = _instance(schema, schema.get("$defs") or schema.get("definitions") or {})
            message["tool_calls"] = [{"id": "call_1", "type": "function",
                                      "function": {"name": function["name"], "arguments": json.dumps(arguments)}}]
            finish = "tool_calls"
        else:
            schema = ((body.get("response_format") or {}).get("json_schema") or {}).get("schema") or \
                _schema_in_messages(body.get("messages") or [])
            message["content"] = json.dumps(_instance(schema, schema.get("$defs") or {}))
            finish = "stop"
        return httpx.Response(200, json={
            "id": "chatcmpl-fake", "object": "chat.completion", "created": 0, "model": "fake-gpt",
            "choices": [{"index": 0, "finish_reason": finish, "message": message}], "usage": usage})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)
