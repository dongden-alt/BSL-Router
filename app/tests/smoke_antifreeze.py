import asyncio, sys
sys.path.insert(0, ".")
from app.antifreeze import afz_guard, force_stop_all, active_stream_count, stream_deadline

# Test 1: hard deadline emits terminal frames
async def endless():
    while True:
        yield b"data: x\n\n"
        await asyncio.sleep(0.05)


async def blocked_source(state):
    try:
        await asyncio.Event().wait()
        yield b"never"
    finally:
        state["closed"] = True


async def main():
    # deadline test
    out = []
    async for c in stream_deadline(endless(), "dl1", deadline_s=0.2):
        out.append(c)
    tail = b"".join(out[-2:])
    print("deadline frames:", tail)
    assert b"[DONE]" in tail, "missing DONE on deadline"
    print("deadline test PASS")

    # blocked-read deadline test
    state = {"closed": False}
    out = [c async for c in stream_deadline(blocked_source(state), "blocked1", deadline_s=0.05)]
    assert b"[DONE]" in b"".join(out[-2:]), "missing DONE on blocked read"
    assert state["closed"], "blocked source was not closed"
    print("blocked-read test PASS")

    # force-stop test
    t = asyncio.create_task(_drain(afz_guard(endless(), "t1")))
    await asyncio.sleep(0.3)
    assert active_stream_count() == 1
    n = await force_stop_all()
    assert n == 1
    await asyncio.sleep(0.2)
    assert active_stream_count() == 0
    print("force-stop test PASS")


async def _drain(gen):
    try:
        async for c in gen:
            pass
    except asyncio.CancelledError:
        pass


asyncio.run(main())
