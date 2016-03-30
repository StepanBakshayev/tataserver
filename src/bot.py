import asyncio
import logging


async def bot():
	reader, writer = await asyncio.open_connection(host='localhost', port=9999)
	writer.write(b'ping\n')
	print(await reader.readline())
	writer.write(b'hello 123\n')
	print(await reader.readline())


def main():
	logging.basicConfig(level=logging.DEBUG)
	loop = asyncio.get_event_loop()
	loop.set_debug(enabled=True)
	try:
		loop.run_until_complete(bot())
	finally:
		loop.close()


if __name__ == '__main__':
	main()