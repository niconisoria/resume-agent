from collections.abc import AsyncIterator

import anthropic

from app.config import ANTHROPIC_API_KEY

MODEL = "claude-haiku-5-5"
MAX_TOKENS = 600

# ponytail: placeholder prompt; full rules arrive with retrieval (S07).
SYSTEM_PROMPT = (
    "You are the assistant on Nicolas Nisoria's resume site. "
    "Speak about Nicolas in the third person. Keep answers brief: "
    "a few sentences. You have no knowledge base yet, so do not invent "
    "facts about his background; say you can't answer that yet."
)

client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


class ReplyRefused(Exception):
    pass


async def stream_reply(message: str) -> AsyncIterator[str]:
    async with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        # Short chat replies: skip thinking so the whole budget goes to text.
        thinking={"type": "disabled"},
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": message}],
    ) as stream:
        async for text in stream.text_stream:
            yield text
        final = await stream.get_final_message()
    if final.stop_reason == "refusal":
        raise ReplyRefused
