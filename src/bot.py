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
import socket
import json
from collections import OrderedDict
import math

async def bot():
	w = '10.0.2.29'
	l = 'localhost'
	sleep = 0
	actions_count = 1000
	reader, writer = await asyncio.open_connection(host=l, port=9999)
	sock = writer.get_extra_info('socket')
	sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
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
	score = 0
	try:
		can_fire = True
		last_message = ''
		x = 0
		for x in range(actions_count):
			if reader.at_eof():
				break

			if can_fire:
				writer.write(b'fire\n')
				can_fire = False
				continue

			await asyncio.sleep(sleep)
			writer.write(b'move %d\n' % randint(0, 3))
			start = perf_counter()
			while True:
				message = (await reader.readline()).decode('utf-8')
				if not message:
					if reader.at_eof():
						break
				assert message
				command, id, *rest = message[:-1].split()
				if id == self_id:
					last_message = ('[%s]' % self_id, command, *rest)
				killed = False
				if id == self_id:
					#print(command, *rest)
					if command == 'score':
						score = int(rest[0], 10)
					elif command == 'missile' and rest[0] == '-':
						can_fire = True
					elif command == 'position':
						end = perf_counter()
						delay.append((end-start)*1000)
						break
	finally:
		if len(delay):
			x += 1
			stats = OrderedDict((
				('avg(ms)', math.floor(sum(delay)/len(delay))),
				('min(ms)', math.floor(min(delay))),
				('max(ms)', math.floor(max(delay))),
				('score()', score),
				('turns(%)', math.floor(x/actions_count*100)),
				('turns()', actions_count),
				('last message', ' '.join(last_message)),
			))
			print(json.dumps(stats))


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