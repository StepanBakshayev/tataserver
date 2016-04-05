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
Player = namedtuple('Player', 'id color x y direction score future_moment')
DEATH_DURATION_MS = 1000
Envinronment = namedtuple('Envinronment', 'id name logger')
Missile = namedtuple('Missile', 'id x y direction future_moment')
MISSILE_VELOCITY_MS = 100
BATTLELOOP_DEADLINE_MS = (1 / 100) * 1000 # 100 fps constant from original implementaion

client_joined_template = 'joined {player.id:d} {player.color:s} {player.x:d} {player.y:d} {player.direction.value:d}\n'
client_id_template = 'id {player.id:d}\n'
client_player_position_template = 'position {player.id:d} {player.x:d} {player.y:d} {player.direction.value:d}\n'
client_player_die_template = 'die {player.id:d} {player.x:d} {player.y:d}\n'
client_player_score_template = 'score {player.id:d} {player.score:d}\n'
client_player_bye_template = 'bye {player.id:d}\n'
client_missile_position_template = 'missile {missile.id:d} {missile.x:d} {missile.y:d} {missile.direction.value:d}\n'
client_missile_destroy_template = 'missile {missile.id:d} -\n'


async def parse_intentions(envinronment, stopped, intentions, reader, writer):
	handlers = ('ping', 'hello', 'move', 'fire')
	pong = envinronment.name.encode('utf-8') + b'\n'
	while not stopped.is_set():
		try:
			future_stop = asyncio.ensure_future(stopped.wait())
			done, pending = await asyncio.wait((reader.readline(), future_stop), return_when=asyncio.FIRST_COMPLETED)
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
		await intentions.join()


class Battle:
	name = 'tataserver'

	def __init__(self, width, height):
		# service data for loop
		self.intentions = asyncio.Queue()
		self.reread_parsers = asyncio.Event()
		self.logger = logging.getLogger('battle')
		self.counter = count()

		# state data
		# ???: what is about to use magic from weakref module to protect data from "holes" (missile without player, player without connection etc)
		# ???: why use dicts here? Is only reason free resources by id?
		self.width = width
		self.height = height
		self.spawn_delta = min(width, height) * 100 // 30
		self.clients_set = set()
		self.connections = {}
		self.parsers = {}
		self.alive_players = {}
		# if dead players is deque swawn algorithm is able to break on first out of time player
		self.dead_players = {}
		self.missiles = {}

	def run_parser(self, id, reader, writer):
		parser_knife_switch = asyncio.Event()
		parser = parse_intentions(
			Envinronment(id=id, name=self.name, logger=self.logger.getChild('connection[%d]' % id)),
			# ???: investigate cancelatoin process to ensure knife-switch is required
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
		 1) iterate intentions and sort in left, moved, fired, joined
		 2) release resources of left players
		 3) let players make turn
		 4) push forward arrived missiles
		 5) detonate missiles on collision
		   + owner of missile takes zero damage
		 6) let playes shoot
		   + show millise on screen and emergency jump to detonation by turning next iteration immediately
		 7) spawn joined players
		 8) spawn dead players

		Consequences are:
		 - lost connections player takes away possible score (it is easy to prevent if I sure about stream read/write process in this case)
		 - player and missile is able to pass by each other in one turn

		Wishes are:
		 - players should push each other on collision
		 - prevent "player and missile is able to pass by each other in one turn"
		"""
		MISSILE_VELOCITY_S = MISSILE_VELOCITY_MS / 1000
		BATTLELOOP_DEADLINE_S = BATTLELOOP_DEADLINE_MS / 1000
		DEATH_DURATION_S = DEATH_DURATION_MS / 1000

		loop = asyncio.get_event_loop()
		intention_future = asyncio.ensure_future(self.intentions.get())
		sleep_future = asyncio.Future()
		wait = (intention_future,)
		while True:
			# if loop exceeded time over deadline these is no wait (don't let switch context for other tasks)
			if wait:
				await asyncio.wait(wait, return_when=asyncio.FIRST_COMPLETED)
			start_time = loop.time()
			# all time base events normalize to this value
			# ???: for good or for evil
			moment = start_time

			# regular future leak
			intention_future.cancel()
			sleep_future.cancel()

			moved = {}
			left = {}
			fired = {}
			joined = {}

			# 1) iterate intentions and sort in left, moved, fired, joined
			if intention_future.done():
				intention = intention_future.result()
				self.intentions.task_done()

				sorter = {
					Action.recognize: joined,
					Action.move: moved,
					Action.terminate: left,
					Action.fire: fired,
				}
				while True:
					self.logger.debug('intention %r', intention)
					sorter[intention.action][intention.id] = intention

					if self.intentions.qsize() == 0:
						break

					intention = self.intentions.get_nowait()
					self.intentions.task_done()

			def ensure_consistency(step, all_players):
				mappings = dict(
					alive_players=self.alive_players,
					dead_players=self.dead_players,
					missiles=self.missiles
				)
				for name, map in mappings.items():
					for key, value in map.items():
						if key != value.id:
							message = '%s: index broken (%r, %r) in "%s" %r'
							args = step, key, value, name, map
							raise RuntimeError(message % args)

				if len(self.alive_players) + len(self.dead_players) < len(all_players):
					id_lost_players = (self.alive_players.keys() | self.dead_players.keys()) ^ all_players.keys()
					lost_players = tuple(all_players[id] for id in id_lost_players)
					intentions = tuple(chain(moved.values(), left.values(), fired.values(), joined.values()))
					message = '%s: players are nowhere %r after intentions %r'
					args = step, lost_players, intentions
					raise RuntimeError(message % args)

				mappings.pop('dead_players')
				for name, map in mappings.items():
					for obj in map.values():
						if obj.x < 0 or obj.x >= self.width or\
							obj.y < 0 or obj.y >= self.height:
							raise RuntimeError('object %r from "%s" is out battle' % (obj, name))

			# 2) release resources of left players
			bye = []
			for intention in left.values():
				id, *_ = intention
				# reverse order from connection_made
				self.clients_set.remove(id)
				reader, writer = self.connections.pop(id)
				reader.feed_eof()
				writer.close()
				parser_knife_switch, parser_future = self.parsers.pop(id)
				# ???: should we cancel parser_future
				parser_knife_switch.set()
				for storage, template in \
					(self.missiles, client_missile_destroy_template),\
					(self.alive_players, client_player_bye_template),\
					(self.dead_players, client_player_bye_template):
					if id in storage:
						value = storage.pop(id)
						bye.append(template.format(missile=value, player=value).encode('utf-8'))

			if bye:
				for _, writer in self.connections.values():
					writer.writelines(bye)

			all_players = self.alive_players.copy()
			all_players.update(self.dead_players.copy())

			ensure_consistency('3) let players make turn', all_players)

			# 3) let players make turn
			for intention in moved.values():
				id, _, direction = intention
				if id not in self.alive_players:
					continue

				player = self.alive_players[id]
				delta = STEP_DELTA[direction]
				x = player.x + delta.x
				if x < 0 or x >= self.width:
					message = client_player_position_template.format(player=player).encode('utf-8')
					writer = self.connections[id][1]
					writer.write(message)
					continue

				y = player.y + delta.y
				if y < 0 or y >= self.height:
					message = client_player_position_template.format(player=player).encode('utf-8')
					writer = self.connections[id][1]
					writer.write(message)
					continue

				player = player._replace(x=x, y=y, direction=direction)
				self.alive_players[id] = player

				message = client_player_position_template.format(player=player).encode('utf-8')
				for _, writer in self.connections.values():
					writer.write(message)

			ensure_consistency('4) push forward arrived missiles', all_players)

			# 4) push forward arrived missiles
			for missile in self.missiles.copy().values():
				if moment < missile.future_moment:
					continue

				delta = STEP_DELTA[missile.direction]
				missile = missile._replace(
					x=missile.x+delta.x, y=missile.y+delta.y,
					future_moment=moment+MISSILE_VELOCITY_S,
				)
				if \
					missile.x < 0 or missile.x >= self.width or\
					missile.y < 0 or missile.y >= self.height:

					del self.missiles[missile.id]

					message = client_missile_destroy_template.format(missile=missile).encode('utf-8')
					for _, writer in self.connections.values():
						writer.write(message)
					continue

				self.missiles[missile.id] = missile
				message = client_missile_position_template.format(missile=missile).encode('utf-8')
				for _, writer in self.connections.values():
					writer.write(message)

			ensure_consistency('5) detonate missiles on collision', all_players)

			# 5) detonate missiles on collision
			for missile in self.missiles.copy().values():
				detonated = []
				for victim in self.alive_players.copy().values():
					if victim.id == missile.id:
						continue

					if victim.x == missile.x and victim.y == missile.y:
						if missile.id not in detonated:
							detonated.append(missile.id)
							del self.missiles[missile.id]
							message_missile_destoy = client_missile_destroy_template.format(missile=missile).encode('utf-8')

						del self.alive_players[victim.id]
						self.dead_players[victim.id] = victim._replace(future_moment=moment+DEATH_DURATION_S)
						message_player_die = client_player_die_template.format(player=victim).encode('utf-8')

						storage = self.alive_players if missile.id in self.alive_players else self.dead_players
						missile_owner = storage[missile.id]
						storage[missile_owner.id] = missile_owner._replace(score=missile_owner.score+1)
						message_player_score = client_player_score_template.format(player=missile_owner).encode('utf-8')

						message = (message_missile_destoy, message_player_die, message_player_score)
						for _, writer in self.connections.values():
							writer.writelines(message)

			ensure_consistency('6) let playes shoot', all_players)

			# 6) let playes shoot
			#   + show millise on screen and emergency jump to detonation by turning next iteration immediately
			shoot = False
			for intention in fired.values():
				id, *_ = intention
				if id in self.dead_players or id in self.missiles:
					continue

				shoot = True

				player = self.alive_players[id]
				delta = STEP_DELTA[player.direction]
				missile = Missile(
					id=player.id, x=player.x, y=player.y, direction=player.direction,
					future_moment=moment+MISSILE_VELOCITY_S,
				)
				self.missiles[id] = missile

				message = client_missile_position_template.format(missile=missile).encode('utf-8')
				for _, writer in self.connections.values():
					writer.write(message)

			ensure_consistency('7) spawn joined players', all_players)

			# 7) spawn joined players
			for intention in joined.values():
				id, _, color = intention
				x, y = self.spawn()
				writer = self.connections[id][1]
				new_player = Player(id, color, x, y, Direction(0), 0, None)
				self.alive_players[id] = new_player

				for player in self.alive_players.values():
					message = client_joined_template.format(player=player).encode('utf-8')
					writer.write(message)
				for player in self.dead_players.values():
					message = client_joined_template.format(player=player).encode('utf-8')
					writer.write(message)
					message = client_player_die_template.format(player=player).encode('utf-8')
					writer.write(message)
				for missile in self.missiles.values():
					message = client_missile_position_template.format(missile=missile).encode('utf-8')
					writer.write(message)

				message = client_id_template.format(player=new_player).encode('utf-8')
				writer.write(message)

				message = client_joined_template.format(player=new_player).encode('utf-8')
				for id in self.clients_set - {id,}:
					writer = self.connections[id][1]
					writer.write(message)

			ensure_consistency('8) spawn dead players', all_players)

			# 8) spawn dead players
			for player in self.dead_players.copy().values():
				if moment < player.future_moment:
					continue

				del self.dead_players[player.id]
				x, y = self.spawn()
				player = player._replace(x=x, y=y, future_moment=None)
				self.alive_players[player.id] = player

				message = client_player_position_template.format(player=player).encode('utf-8')
				for _, writer in self.connections.values():
					writer.write(message)

			ensure_consistency('end', all_players)

			# crude loop schedule
			wait = ()
			intention_future = asyncio.ensure_future(self.intentions.get())
			if not shoot:
				wait += (intention_future,)
				sleep_future = asyncio.Future()
				event_sources = (self.missiles, self.dead_players)
				if any(event_sources):
					next_source = min(chain.from_iterable(source.values() for source in event_sources), key=lambda v: v.future_moment)
					sleep_future = asyncio.ensure_future(asyncio.sleep(next_source.future_moment-loop.time()))
					wait += (sleep_future,)

			end_time = loop.time()
			time_ms = end_time - start_time
			if time_ms > BATTLELOOP_DEADLINE_S:
				self.logger.warning('loop exceed time: it took %d ms', time_ms*1000)

	async def parser_failover(self):
		while True:
			future_reread = asyncio.ensure_future(self.reread_parsers.wait())
			parsers_future = tuple(p[1] for p in self.parsers.values())
			await asyncio.wait((future_reread,)+parsers_future, return_when=asyncio.FIRST_COMPLETED)

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
			((player.x, player.y) for player in self.alive_players.values()),
			((missile.x, missile.y) for missile in self.missiles.values()),
		)
		for holded_x, holded_y in sorted(holded_points, key=lambda v: v[0]+v[1]*self.height):
			if holded_x == x and holded_y == y:
				x += randint(1, self.spawn_delta)
				y += randint(1, self.spawn_delta) * choice((-1, 1))
				x = min(0, x, self.width-1)
				y = min(0, abs(y), self.height-1)

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
