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
import functools
import os
import signal
from enum import Enum
from collections import namedtuple
from itertools import count
import concurrent.futures
from random import randrange, choice, randint
import logging

Action = Enum('Action', 'recognize move fire terminate', start=0)
Direction = Enum('Direction', 'north west south east', start=0)
STEP_DELTA = {
		Direction.north: (0, 1),
	Direction.west: (-1, 0), Direction.east: (1, 0),
		Direction.south: (0, -1)
}
Intention = namedtuple('Intention', 'id action value')
Player = namedtuple('Player', 'id color x y direction')


class Client:
	joined_template = 'joined {player.id:d} {player.color:s} {player.x:d} {player.y:d} {player.direction.value:d}\n'
	id_template = 'id {player.id:d}\n'
	position_template = 'position {player.id:d} {player.x:d} {player.y:d} {player.direction.value:d}\n'

	def __init__(self, name, id, intentions, reader, writer):
		self.pong = name.encode('utf-8') + b'\n'
		self.id = id
		self.intentions = intentions
		self.reader = reader
		self.writer = writer

		self.stopped = False

	async def process(self):
		handlers = ('ping', 'hello', 'move', 'fire')
		while not self.stopped:
			try:
				command = (await self.reader.readline()).decode('utf-8')
				if not command:
					if self.reader.at_eof():
						raise EOFError
					continue
			except Exception as e:
				self.intentions.put_nowait(Intention(self.id, Action.terminate, e))
				break

			logging.info('command %r', command)
			name, *values = command[:-1].split(' ')
			if name not in handlers:
				continue

			if name == 'ping':
				self.writer.write(self.pong)
			elif name == 'hello':
				color, *_ = values
				self.intentions.put_nowait(Intention(self.id, Action.recognize, color))
			elif name == 'move':
				direction, *_ = values
				direction = int(direction, 10)
				self.intentions.put_nowait(Intention(self.id, Action.move, Direction(direction)))
			elif name == 'fire':
				self.intentions.put_nowait(Intention(self.id, Action.fire, None))


class Battle:
	name = 'PY_battle'

	def __init__(self, width, height):
		self.width = width
		self.height = height
		self.spawn_delta = min(width, height) * 100 # 30
		self.intentions = asyncio.Queue()
		self.counter = count()
		self.clients = {}
		self.clients_set = set()
		self.players = {}
		self.stopped = asyncio.Event()

	def connection_made(self, reader, writer):
		id = next(self.counter)
		client = Client(self.name, id, self.intentions, reader, writer)
		self.clients[id] = client
		self.clients_set.add(id)
		asyncio.ensure_future(client.process())

	async def loop(self):
		while True:
			done, _ = await asyncio.wait((self.stopped.wait(), self.intentions.get()), return_when=concurrent.futures.FIRST_COMPLETED)
			if len(done) == 2 or True in done:
				for client in self.clients.values():
					client.stopped = True
				break

			intention = done.pop().result()
			moved = []
			left = []
			joined = []
			sorter = {Action.recognize: joined, Action.move: moved, Action.terminate: left}
			clients_maked_turn = []
			while True:
				logging.info('intention %r', intention)
				if intention.action not in clients_maked_turn or intention.action == Action.terminate:
					if intention.action in sorter:
						sorter[intention.action].append(intention)
						clients_maked_turn.append(intention.id)
				try:
					intention = self.intentions.get_nowait()
				except asyncio.QueueEmpty:
					break

			for intention in moved:
				id = intention.id
				direction = intention.value
				player = self.players[id]
				delta = STEP_DELTA[direction]
				client = self.clients[id]

				x = player.x + delta[0]
				if x < 0 or x >= self.width:
					message = (Client.position_template.format(player=player)).encode('utf-8')
					client.writer.write(message)
					continue
				y = player.y + delta[1]
				if y < 0 or y >= self.height:
					message = (Client.position_template.format(player=player)).encode('utf-8')
					client.writer.write(message)
					continue
				player = player._replace(x=x, y=y, direction=direction)
				self.players[id] = player
				message = (Client.position_template.format(player=player)).encode('utf-8')
				for id in self.clients_set:
					self.clients[id].writer.write(message)

			for intention in left:
				id = intention.id
				if id in self.players:
					del self.players[id]
				self.clients_set.remove(id)
				client = self.clients.pop(id)
				client.stopped = True

			for intention in joined:
				id = intention.id
				x, y = self.spawn()
				client = self.clients[id]

				new_player = Player(id, intention.value, x, y, Direction(0))
				self.players[id] = new_player

				for player in self.players.values():
					message = Client.joined_template.format(player=player)
					client.writer.write(message.encode('utf-8'))

				id_message = Client.id_template.format(player=new_player)
				client.writer.write(id_message.encode('utf-8'))

				joined_message = Client.joined_template.format(player=new_player)
				message = joined_message.encode('utf-8')
				for id in self.clients_set - {id,}:
					self.clients[id].writer.write(message)

	def spawn(self):
		x = randrange(0, self.width)
		y = randrange(0, self.height)
		for player in self.players.values():
			if player.x == x and player.y == y:
				x += randint(1, self.spawn_delta) * choice(-1, 1)
				y += randint(1, self.spawn_delta) * choice(-1, 1)
				x = min(0, x, self.width)
				y = min(0, y, self.height)
		return x, y


async def manage_battle():
	battle = Battle(16, 16)
	server = await asyncio.start_server(battle.connection_made, port=9999)
	asyncio.ensure_future(battle.loop())
	await server.wait_closed()
	battle.stopped.set()


def ask_exit(loop, signame):
	print("got signal %s: exit" % signame)
	loop.stop()


def main():
	logging.basicConfig(level=logging.DEBUG)
	loop = asyncio.get_event_loop()
	loop.set_debug(enabled=True)
	for signame in ('SIGINT', 'SIGTERM'):
		loop.add_signal_handler(getattr(signal, signame), functools.partial(ask_exit, loop, signame))
	try:
		asyncio.ensure_future(manage_battle())
		loop.run_forever()
	finally:
		loop.close()


if __name__ == '__main__':
	main()
