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
from time import perf_counter


async def bot():
	w = '10.0.2.29'
	l = 'localhost'
	sleep = 0
	fire_frequency = 5
	actions_count = 1000
	reader, writer = await asyncio.open_connection(host=l, port=9999)
	writer.write(b'ping\n')
	pong = await reader.readline()
	assert pong
	writer.write(b'hello 255\n')
	while True:
		message = (await reader.readline()).decode('utf-8')
		assert message
		if message.startswith('id'):
			_, self_id = message[:-1].split(' ')
			break

	delay = []
	try:
		for x in range(actions_count):
			await asyncio.sleep(sleep)
			if randint(0, fire_frequency) == fire_frequency:
				writer.write(b'fire\n')
				continue
			writer.write(b'move %d\n' % randint(0, 3))
			start = perf_counter()
			while True:
				message = (await reader.readline()).decode('utf-8')
				assert message
				command, id, *_ = message[:-1].split()
				#print(command, id, *_)
				if command == 'position' and id == self_id:
					end = perf_counter()
					delay.append((end-start)*1000)
					break
	finally:
		if len(delay):
			print('avg(ms):', sum(delay)/len(delay), 'min(ms):', min(delay), 'max(ms):', max(delay))


def main():
	logging.basicConfig(level=logging.DEBUG)
	loop = asyncio.get_event_loop()
	loop.set_debug(enabled=False)
	try:
		loop.run_until_complete(bot())
	finally:
		loop.close()


if __name__ == '__main__':
	main()