# Copyright 2016 Stepan Bakshayev. See the COPYRIGHT
# file at the top-level directory of this distribution and at
# http://rust-lang.org/COPYRIGHT.
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# http://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or http://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.

import asyncio
import logging
from random import randint


async def bot():
	w = '10.0.2.29'
	l = 'localhost'
	reader, writer = await asyncio.open_connection(host=w, port=9999)
	writer.write(b'ping\n')
	print(await reader.readline())
	writer.write(b'hello 255\n')
	while True:
		message = (await reader.readline()).decode('utf-8')
		print(message)
		if message.startswith('id'):
			_, self_id = message[:-1].split(' ')
			break

	for x in range(1000):
		await asyncio.sleep(0.1)
		writer.write(b'move %d\n' % randint(0, 3))
		while True:
			message = (await reader.readline()).decode('utf-8')
			print(message)
			command, id, *_ = message.split()
			if command == 'position' and id == self_id:
				break



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