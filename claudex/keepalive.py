"""Prompt-scoped ACP progress for hosts with a silence watchdog."""
import asyncio
import time


class PromptActivity:
    def __init__(self, update, interval=30, clock=time.monotonic, sleep=asyncio.sleep):
        self.update, self.interval = update, interval
        self.clock, self.sleep = clock, sleep
        self.last_update = clock()
        self.task = None

    def touch(self):
        self.last_update = self.clock()

    async def start(self):
        await self.stop()
        await self.update({"sessionUpdate": "agent_message_chunk",
                           "content": {"type": "text", "text": "Planning…\n\n"}})
        self.touch()
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        while True:
            await self.sleep(self.interval)
            if self.clock() - self.last_update >= self.interval:
                # AoE counts thoughts as progress; keep them out of the main reply.
                await self.update({"sessionUpdate": "agent_thought_chunk",
                                   "content": {"type": "text", "text": "Still working…\n"}})
                self.touch()

    async def stop(self):
        task, self.task = self.task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
