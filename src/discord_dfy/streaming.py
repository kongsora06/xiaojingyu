"""Turn a streamed chat completion back into a normal one while feeding the Live Monitor."""

from types import SimpleNamespace


async def collect_stream(stream, publisher, request_id):
    """Rebuild a chat completion from deltas while mirroring thinking/text to the monitor."""
    content, reasoning, calls = [], [], {}
    usage = None
    publisher.event("model-status", status="started", request_id=request_id)
    try:
        async for chunk in stream:
            if chunk.usage is not None:
                usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            thought = getattr(delta, "reasoning_content", None)
            if isinstance(thought, str) and thought:
                reasoning.append(thought)
                publisher.text("reasoning-delta", thought, request_id=request_id)
            if isinstance(delta.content, str) and delta.content:
                publisher.text("reasoning-delta", request_id=request_id, final=True)
                content.append(delta.content)
                publisher.text("text-delta", delta.content, request_id=request_id)
            for part in delta.tool_calls or []:
                call = calls.setdefault(part.index, {"id": "", "name": "", "arguments": []})
                call["id"] = part.id or call["id"]
                if part.function is not None:
                    call["name"] += part.function.name or ""
                    call["arguments"].append(part.function.arguments or "")
    finally:
        await stream.close()
        publisher.flush()
    publisher.event("model-status", status="completed", request_id=request_id)
    message = SimpleNamespace(
        content="".join(content),
        reasoning_content="".join(reasoning) or None,
        tool_calls=[
            SimpleNamespace(
                id=call["id"],
                function=SimpleNamespace(name=call["name"], arguments="".join(call["arguments"])),
            )
            for _, call in sorted(calls.items())
        ],
    )
    return SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=message)])
