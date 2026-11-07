import asyncio
from types import SimpleNamespace

import pytest

from claudex.keepalive import PromptActivity
from claudex.server import Server, SessionUI


class Clock:
    def __init__(self):
        self.now = 0
        self.waiters = asyncio.Queue()

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        await self.waiters.put((self.now + seconds, future))
        await future

    async def advance(self):
        deadline, future = await self.waiters.get()
        self.now = deadline
        future.set_result(None)
        await asyncio.sleep(0)


def test_quiet_progress_and_cleanup_with_clock():
    async def run():
        clock, updates = Clock(), []

        async def update(value):
            updates.append(value)

        activity = PromptActivity(update, clock=lambda: clock.now, sleep=clock.sleep)
        await activity.start()
        assert updates[0]['content']['text'] == 'Planning…\n\n'
        for _ in range(5):
            await clock.advance()
        assert clock.now == 150
        assert [u['sessionUpdate'] for u in updates] == ['agent_message_chunk'] + ['agent_thought_chunk'] * 5
        await asyncio.sleep(0)
        clock.now += 1
        activity.touch()
        await clock.advance()
        assert len(updates) == 6  # recent worker output needs no heartbeat
        task = activity.task
        await activity.stop()
        assert task.done() and activity.task is None
        count = len(updates)
        await asyncio.sleep(0)
        assert len(updates) == count
        await activity.stop()
    asyncio.run(run())


@pytest.mark.parametrize('ending', ['finish', 'error', 'cancel'])
def test_server_owns_timer_until_prompt_ends(ending):
    async def run():
        updates, started = [], asyncio.Event()
        ui = SessionUI(SimpleNamespace(notify=lambda method, params: updates.append(params['update'])), 's')
        clock = Clock()
        ui.activity.clock = lambda: clock.now
        ui.activity.sleep = clock.sleep

        result = asyncio.get_running_loop().create_future()

        async def turn(text):
            assert updates[0]['content']['text'].startswith('Planning')
            started.set()
            return await result

        engine = SimpleNamespace(ui=ui, busy=False, turn=turn, cwd='.', skills={})
        server = Server()
        server.sessions['s'] = engine
        prompt = asyncio.create_task(server.on_request('session/prompt', {'sessionId': 's', 'prompt': []}))
        await started.wait()
        timer = ui.activity.task
        await clock.advance()
        if ending == 'cancel':
            engine.cancel = prompt.cancel
            await server.on_notify('session/cancel', {'sessionId': 's'})
            with pytest.raises(asyncio.CancelledError):
                await prompt
        else:
            if ending == 'error':
                result.set_exception(RuntimeError('failed'))
                with pytest.raises(RuntimeError, match='failed'):
                    await prompt
            else:
                result.set_result(None)
                assert await prompt == {'stopReason': None}
        assert timer.done() and ui.activity.task is None and not engine.busy
        count = len(updates)
        await asyncio.sleep(0)
        assert len(updates) == count
    asyncio.run(run())
