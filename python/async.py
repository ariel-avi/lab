import asyncio
import time

SLEEP_S = 2

async def long_task():
    await asyncio.sleep(SLEEP_S)
    return 0

async def main():
    l1 = long_task()
    l2 = long_task()
    l3 = long_task()

    await asyncio.gather(l1, l2, l3)


if __name__ == '__main__':
    start = time.time()
    asyncio.run(main())
    end = time.time()
    assert end - start < SLEEP_S + 0.1
