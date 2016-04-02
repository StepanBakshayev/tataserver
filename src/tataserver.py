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
from itertools import count, chain
import concurrent.futures
from random import randrange, choice, randint
import logging


Action = Enum('Action', 'recognize move fire terminate', start=0)
Direction = Enum('Direction', 'north west south east', start=0)
Delta = namedtuple('Delta', 'x y')
STEP_DELTA = {
		Direction.north: Delta(0, 1),
	Direction.west: Delta(-1, 0), Direction.east: Delta(1, 0),
		Direction.south: Delta(0, -1)
}
Intention = namedtuple('Intention', 'id action value')
Player = namedtuple('Player', 'id color x y direction')
DEATH_DURATION_MS = 1000
Envinronment = namedtuple('Envinronment', 'id name logger')
Missile = namedtuple('Missile', 'id x y direction moment to_x to_y')
MISSILE_VELOCITY_MS = 100
BATTLELOOP_DEADLINE_MS = (1 / 100) * 1000 # 100 fps constant from original implementaion

client_joined_template = 'joined {player.id:d} {player.color:s} {player.x:d} {player.y:d} {player.direction.value:d}\n'
client_id_template = 'id {player.id:d}\n'
client_position_template = 'position {player.id:d} {player.x:d} {player.y:d} {player.direction.value:d}\n'
client_bye_template = 'bye {player.id:d}\n'
client_missile_position_template = 'missile {missile.id:d} {missile.x:d} {missile.y:d} {missile.direction.value:d}\n'
client_missile_destroy_template = 'missile {missile.id:d} -\n'


async def parse_intentions(envinronment, stopped, intentions, reader, writer):
	handlers = ('ping', 'hello', 'move', 'fire')
	pong = envinronment.name.encode('utf-8') + b'\n'
	while not stopped.is_set():
		try:
			future_stop = asyncio.ensure_future(stopped.wait())
			done, pending = await asyncio.wait((reader.readline(), future_stop), return_when=concurrent.futures.FIRST_COMPLETED)
			# stopped anyway
			if not pending:
				return

			# prevent futures leak through the accumulation of Event._waiter
			future_stop.cancel()

			result = done.pop().result()
			# stopped answer
			if result is True:
				return

			command = result.decode('utf-8')
			if not command:
				if reader.at_eof():
					raise EOFError
				continue
			envinronment.logger.debug('command %r', command)

		except Exception as e:
			envinronment.logger.debug('terminated with %r', e)
			intentions.put_nowait(Intention(envinronment.id, Action.terminate, e))
			break

		name, *values = command[:-1].split(' ')
		if name not in handlers:
			envinronment.logger.debug('name %r was not recognized', name)
			continue

		if name == 'ping':
			envinronment.logger.debug('pong')
			writer.write(pong)
			continue

		if name == 'hello':
			color, *_ = values
			intention = Intention(envinronment.id, Action.recognize, color)
		elif name == 'move':
			direction, *_ = values
			direction = int(direction, 10)
			intention = Intention(envinronment.id, Action.move, Direction(direction))
		elif name == 'fire':
			intention = Intention(envinronment.id, Action.fire, None)
		else:
			envinronment.logger.warning('programming error: command name %r was not handled', name)
			continue

		envinronment.logger.debug('command parsed as %r', intention)
		intentions.put_nowait(intention)


class Battle:
	name = 'tataserver'

	def __init__(self, width, height):
		self.width = width
		self.height = height
		self.spawn_delta = min(width, height) * 100 // 30

		self.intentions = asyncio.Queue()
		self.reread_parsers = asyncio.Event()

		self.counter = count()
		self.clients_set = set()
		self.connections = {}
		self.parsers = {}
		self.players = {}
		self.missiles = {}
		self.logger = logging.getLogger('battle')

	def run_parser(self, id, reader, writer):
		parser_knife_switch = asyncio.Event()
		parser = parse_intentions(
			Envinronment(id=id, name=self.name, logger=self.logger.getChild('connection[%d]' % id)),
			parser_knife_switch,
			self.intentions,
			reader,
			writer
		)
		parser_future = asyncio.ensure_future(parser)
		return parser_knife_switch, parser_future

	def connection_made(self, reader, writer):
		id = next(self.counter)
		(parser_knife_switch, parser_future) = self.run_parser(id, reader, writer)
		self.parsers[id] = (parser_knife_switch, parser_future)
		self.connections[id] = (reader, writer)
		self.clients_set.add(id)

		self.reread_parsers.set()

	async def loop(self):
		"""
		Loop implementaion is game rules. Game rules are:
		 1) terminated connection free all scheduled intentaions
		 2) clean data from terminated connections
		 3) let players move
		 4) missiles fly
		 5) spawn fired missiles
		 6) spawn joined playes
		 7) spawn dead players
		"""
		loop = asyncio.get_event_loop()
		# some big number
		fly_after = 1
		MISSILE_VELOCITY_S = MISSILE_VELOCITY_MS / 1000
		while True:
			self.logger.debug('next fly time in %d ms', fly_after * 1000)
			fly_future = asyncio.ensure_future(asyncio.sleep(fly_after))
			intention_future = asyncio.ensure_future(self.intentions.get())
			await asyncio.wait((fly_future, intention_future), return_when=concurrent.futures.FIRST_COMPLETED)
			start_time = loop.time()

			# regular future leak
			intention_future.cancel()
			fly_future.cancel()

			moved = {}
			left = {}
			fired = {}
			joined = {}

			if intention_future.done():
				intention = intention_future.result()
				sorter = {
					Action.recognize: joined,
					Action.move: moved,
					Action.terminate: left,
					Action.fire: fired,
				}
				clients_maked_turn = []
				# intentions are not only things happen on battle field
				# so delay it to next time, but increase CPU utilization
				while (loop.time() - start_time) * 1000 < BATTLELOOP_DEADLINE_MS:
					self.logger.info('intention %r', intention)

					if intention.action == Action.terminate:
						sorter[Action.terminate][intention.id] = intention
						# it is useless to write in closed connection
						# avoid it by removing all scheduled intentions
						if intention.id in clients_maked_turn:
							for action in (Action.recognize, Action.move, Action.fire):
								if intention.id in sorter[action]:
									del sorter[action][intention.id]
						clients_maked_turn.append(intention.id)

					elif intention.id not in clients_maked_turn and intention.action in sorter:
						sorter[intention.action][intention.id] = intention
						clients_maked_turn.append(intention.id)

					try:
						intention = self.intentions.get_nowait()
					except asyncio.QueueEmpty:
						break

			bye = bytearray()
			for intention in left.values():
				id = intention.id
				# reverse order from connection_made
				self.clients_set.remove(id)
				reader, writer = self.connections.pop(id)
				reader.feed_eof
				writer.close()
				parser_knife_switch, parser_future = self.parsers.pop(id)
				# ???: should we cancel parser_future
				parser_knife_switch.set()
				if id in self.players:
					bye += client_bye_template.format(player=self.players.pop(id)).encode('utf-8')
				if id in self.missiles:
					bye += client_missile_destroy_template.format(missile=self.missiles.pop(id)).encode('utf-8')

			if bye:
				for _, writer in self.connections.values():
					writer.write(bye)

			for intention in moved.values():
				id = intention.id
				direction = intention.value
				player = self.players[id]
				delta = STEP_DELTA[direction]

				x = player.x + delta.x
				if x < 0 or x >= self.width:
					message = (client_position_template.format(player=player)).encode('utf-8')
					writer = self.connections[id][1]
					writer.write(message)
					continue
				y = player.y + delta.y
				if y < 0 or y >= self.height:
					message = (client_position_template.format(player=player)).encode('utf-8')
					writer = self.connections[id][1]
					writer.write(message)
					continue
				player = player._replace(x=x, y=y, direction=direction)

				self.players[id] = player
				message = (client_position_template.format(player=player)).encode('utf-8')
				for _, writer in self.connections.values():
					writer.write(message)

			stop_time = loop.time()
			for missile in self.missiles.copy().values():
				if (stop_time - (missile.moment + MISSILE_VELOCITY_S)) < 0:
					continue
				delta = STEP_DELTA[missile.direction]
				missile = missile._replace(
					x=missile.to_x, y=missile.to_y,
					moment=stop_time,
					to_x=missile.to_x+delta.x, to_y=missile.to_y+delta.y
				)
				if \
					missile.x < 0 or missile.x >= self.width or\
					missile.y < 0 or missile.y >= self.height:
					del self.missiles[missile.id]

					message = (client_missile_destroy_template.format(missile=missile)).encode('utf-8')
					for _, writer in self.connections.values():
						writer.write(message)
					continue

				self.missiles[missile.id] = missile
				message = (client_missile_position_template.format(missile=missile)).encode('utf-8')
				for _, writer in self.connections.values():
					writer.write(message)

			stop_time = loop.time()
			for intention in fired.values():
				if intention.id in self.missiles:
					continue
				id = intention.id
				player = self.players[id]
				delta = STEP_DELTA[player.direction]
				missile = Missile(
					id=player.id, x=player.x, y=player.y, direction=player.direction,
					moment=stop_time,
					to_x=player.x+delta.x, to_y=player.y+delta.y
				)

				self.missiles[id] = missile
				message = (client_missile_position_template.format(missile=missile)).encode('utf-8')
				for _, writer in self.connections.values():
					writer.write(message)

			for intention in joined.values():
				id = intention.id
				x, y = self.spawn()
				writer = self.connections[id][1]

				new_player = Player(id, intention.value, x, y, Direction(0))
				self.players[id] = new_player

				for player in self.players.values():
					message = client_joined_template.format(player=player)
					writer.write(message.encode('utf-8'))

				id_message = client_id_template.format(player=new_player)
				writer.write(id_message.encode('utf-8'))

				joined_message = client_joined_template.format(player=new_player)
				message = joined_message.encode('utf-8')
				for id in self.clients_set - {id,}:
					writer = self.connections[id][1]
					writer.write(message)

			fly_after = 1
			if self.missiles:
				next_moment = min((missile.moment for missile in self.missiles.values()))
				fly_after = (next_moment + MISSILE_VELOCITY_MS / 1000) - loop.time()

			end_time = loop.time()
			time_ms = (end_time - start_time) * 1000
			if time_ms > BATTLELOOP_DEADLINE_MS:
				self.logger.warning('loop exceed time: it took %d ms', time_ms)


	async def parser_failover(self):
		while True:
			future_reread = asyncio.ensure_future(self.reread_parsers.wait())
			parsers_future = tuple(p[1] for p in self.parsers.values())
			await asyncio.wait((future_reread,)+parsers_future, return_when=concurrent.futures.FIRST_COMPLETED)

			# prevent futures leak through the accumulation of Event._waiter
			future_reread.cancel()

			for id, p in self.parsers.copy().items():
				task = p[1]
				# FIXME: handle case when parser exits normally, but connection and player are here
				if not task.cancelled() and task.done() and task.exception():
					self.logger.warning('failover parser %d from exception %r', id, task.exception())
					(parser_knife_switch, parser_future) = self.run_parser(id, *self.connections[id])
					self.parsers[id] = (parser_knife_switch, parser_future)

			self.reread_parsers.clear()

	def spawn(self):
		"""
		Return random point on battlefield, try easy to avoid players and missiles
		"""
		x = randrange(0, self.width)
		y = randrange(0, self.height)
		holded_points = chain(
			((player.x, player.y) for player in self.players.values()),
			((missile.x, missile.y) for missile in self.missiles.values()),
			((missile.to_x, missile.to_y) for missile in self.missiles.values()),
		)
		for holded_x, holded_y in sorted(holded_points, key=lambda v: v[0]+v[1]*self.height):
			if holded_x == x and holded_y == y:
				x += randint(1, self.spawn_delta)
				y += randint(1, self.spawn_delta) * choice((-1, 1))
				x = min(0, x, self.width)
				y = min(0, y, self.height)

		return x, y


async def manage_battle():
	battle = Battle(16, 16)
	await asyncio.start_server(battle.connection_made, port=9999)
	asyncio.ensure_future(battle.loop())
	asyncio.ensure_future(battle.parser_failover())


def ask_exit(loop, signame):
	print("got signal %s: exit" % signame)
	loop.stop()


def main():
	logging.basicConfig(level=logging.WARNING)
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
