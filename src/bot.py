# Copyright 2016 Stepan Bakshayev. See the COPYRIGHT
# file at the top-level directory of this distribution and at
# http://rust-lang.org/COPYRIGHT.
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# http://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or http://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.

from collections import OrderedDict
from random import randint
from time import perf_counter
import asyncio
import json
import logging
import math
import socket
import os

async def bot():
	w = '10.0.2.29'
	l = 'localhost'
	n = '0.tcp.eu.ngrok.io'
	p0 = 9999
	pn = 12825
	timeout = 10
	sleep = 0
	actions_count = 1000
	reader, writer = await asyncio.open_connection(host=l, port=p0)
	sock = writer.get_extra_info('socket')
	sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

	writer.write(b'ping\n')
	pong = await reader.readline()
	assert pong
	print('<<%s>> first message %r' % (os.getpid(), pong))

	writer.write(b'hello 255\n')
	self_id = None
	start_join = perf_counter()
	spawn_time = None
	while True:
		bytes = await asyncio.wait_for(reader.readline(), timeout)
		message = bytes.decode('utf-8')
		assert message
		command, id, *rest = message[:-1].split()
		if command == 'id':
			end_join = perf_counter()
			self_id = id
			spawn_time = (end_join - start_join) * 1000
			break

	delay = []
	score = 0
	print('<<%s>> identifier %r' % (os.getpid(), self_id))
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
				bytes = await asyncio.wait_for(reader.readline(), timeout)
				message = bytes.decode('utf-8')
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
					elif command == 'die':
						killed = True
					elif command == 'position':
						end = perf_counter()
						if not killed:
							delay.append((end-start)*1000)
						break
	finally:
		if len(delay):
			x += 1
			stats = OrderedDict((
				('move', OrderedDict((
					('avg(ms)', math.floor(sum(delay)/len(delay))),
					('min(ms)', math.floor(min(delay))),
					('max(ms)', math.floor(max(delay))),
					('count', len(delay)),
				))),
				('join delay(ms)', math.floor(spawn_time)),
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