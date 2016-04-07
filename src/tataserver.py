# Copyright 2016 Stepan Bakshayev. See the COPYRIGHT
# file at the top-level directory of this distribution and at
# http://rust-lang.org/COPYRIGHT.
#
# Licensed under the Apache License, Version 2.0 <LICENSE-APACHE or
# http://www.apache.org/licenses/LICENSE-2.0> or the MIT license
# <LICENSE-MIT or http://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.

from collections import namedtuple
from enum import Enum
from itertools import count, chain
from random import randrange, choice, randint
import asyncio
import functools
import inspect
import logging
import signal
import socket


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

# implementation specific time resolution
MISSILE_VELOCITY_S = MISSILE_VELOCITY_MS / 1000
BATTLELOOP_DEADLINE_S = BATTLELOOP_DEADLINE_MS / 1000
DEATH_DURATION_S = DEATH_DURATION_MS / 1000


client_joined_template = 'joined {player.id:d} {player.color:s} {player.x:d} {player.y:d} {player.direction.value:d}\n'
client_id_template = 'id {player.id:d}\n'
client_player_position_template = 'position {player.id:d} {player.x:d} {player.y:d} {player.direction.value:d}\n'
client_player_die_template = 'die {player.id:d} {player.x:d} {player.y:d}\n'
client_player_score_template = 'score {player.id:d} {player.score:d}\n'
client_player_bye_template = 'bye {player.id:d}\n'
client_missile_position_template = 'missile {missile.id:d} {missile.x:d} {missile.y:d} {missile.direction.value:d}\n'
client_missile_destroy_template = 'missile {missile.id:d} -\n'

DEBUG = False

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
		self.connections = {}
		self.alive_connections = []
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
		sock = writer.get_extra_info('socket')
		sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

		id = next(self.counter)
		(parser_knife_switch, parser_future) = self.run_parser(id, reader, writer)
		self.parsers[id] = (parser_knife_switch, parser_future)
		self.connections[id] = (reader, writer)

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
		 7) spawn dead players
		 8) spawn joined players

		Consequences are:
		 - lost connections player takes away possible score (it is easy to prevent if I sure about stream read/write process in this case)
		 - player and missile is able to pass by each other in one turn

		Wishes are:
		 - players should push each other on collision
		 - prevent "player and missile is able to pass by each other in one turn"
		"""
		loop = asyncio.get_event_loop()
		intention_future = asyncio.ensure_future(self.intentions.get())
		sleep_future = asyncio.Future()
		soon_future = asyncio.Future()
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
			soon_future.cancel()

			moved = []
			left = []
			fired = []
			joined = []

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
					sorter[intention.action].append(intention)

					if self.intentions.qsize() == 0:
						break

					intention = self.intentions.get_nowait()
					self.intentions.task_done()

			# 2) release resources of left players
			battle_clean_from(left, self.connections, self.alive_connections, self.parsers, self.alive_players, self.dead_players, self.missiles)

			for writer in self.alive_connections:
				sock = writer.get_extra_info('socket')
				if sock.fileno() != -1:
					sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_CORK, 1)

			# 3) let players make turn
			battle_transfer(moved, self.alive_players, self.width, self.height, self.connections, self.alive_connections)

			# 4) push forward arrived missiles
			battle_push(moment, self.missiles, self.width, self.height, self.alive_connections)

			# 5) detonate missiles on collision
			battle_blow_up(moment, self.missiles, self.alive_players, self.dead_players, self.alive_connections)

			# 6) let playes shoot
			#   + show millise on screen and emergency jump to detonation by turning next iteration immediately
			shoot = battle_pull_the_trigger(moment, fired, self.missiles, self.alive_players, self.dead_players, self.alive_connections)

			# 7) spawn dead players
			battle_spawn(moment, self.dead_players, self.width, self.height, self.spawn_delta, self.alive_players, self.missiles, self.alive_connections)

			# 8) spawn joined players
			new_alive_connections = battle_recognize(joined, self.width, self.height, self.spawn_delta, self.alive_players, self.missiles, self.dead_players, self.connections, self.alive_connections)

			for writer in self.alive_connections:
				sock = writer.get_extra_info('socket')
				if sock.fileno() != -1:
					sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_CORK, 0)
			self.alive_connections.extend(new_alive_connections)

			# crude loop schedule
			wait = ()
			intention_future = asyncio.ensure_future(self.intentions.get())
			if shoot:
				soon_future = asyncio.Future()
				soon_future._loop.call_soon(soon_future._set_result_unless_cancelled, None)
				wait += (soon_future,)
			else:
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
				#self.logger.warning('loop exceed time: it took %d ms', time_ms*1000)
				pass

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


def battle_ensure_consistency(function):
	if not DEBUG:
		return function

	signature = inspect.signature(function)
	signature_args = dict((parameter.name, i) for i, parameter in enumerate(signature.parameters.values()) if parameter.kind == parameter.POSITIONAL_OR_KEYWORD)
	assert len(signature.parameters) == len(signature_args), 'Decorator accepts only position function calc %r, source %r' % (signature_args, signature.parameters)
	step = 'after %s' % function.__qualname__

	@functools.wraps(function)
	def wrapper(*args):
		player_mappings = {'alive_players', 'dead_players'}
		all_players = {}
		if signature_args.keys() >= player_mappings:
			for name in player_mappings:
				all_players.update(args[signature_args[name]])

		result = function(*args)

		index_mappings = {'alive_players', 'dead_players', 'missiles'}
		index_signature = signature_args.keys() & index_mappings
		if index_signature:
			for name in index_signature:
				for key, value in args[signature_args[name]].items():
					if key != value.id:
						message = '%s: index broken (%r, %r) in "%s"'
						args = step, key, value, name
						raise RuntimeError(message % args)

		if signature_args.keys() >= {'width', 'height'}:
			width, height = args[signature_args['width']], args[signature_args['height']]
			for name in signature_args.keys() & {'missiles', 'alive_players'}:
				for obj in args[signature_args[name]].values():
					if obj.x < 0 or obj.x >= width or\
						obj.y < 0 or obj.y >= height:
						message = '%s: object %r from "%s" is out battle'
						args = step, obj, name
						raise RuntimeError(message % args)

		if signature_args.keys() >= player_mappings:
			alive_players, dead_players = args[signature_args['alive_players']], args[signature_args['dead_players']]
			if len(alive_players) + len(dead_players) < len(all_players):
				id_lost_players = (alive_players.keys() | dead_players.keys()) ^ all_players.keys()
				lost_players = tuple(all_players[id] for id in id_lost_players)
				intentions = tuple(chain(moved.values(), left.values(), fired.values(), joined.values()))
				message = '%s: players are nowhere %r'
				args = step, lost_players
				raise RuntimeError(message % args)

		return result

	return wrapper


def battle_clean_from(left, connections, alive_connections, parsers, alive_players, dead_players, missiles):
	bye = []
	for intention in left:
		id, *_ = intention
		# reverse order from connection_made
		reader, writer = connections.pop(id)
		reader.feed_eof()
		writer.close()
		parser_knife_switch, parser_future = parsers.pop(id)
		# ???: should we cancel parser_future
		parser_knife_switch.set()
		if writer in alive_connections:
			alive_connections.remove(writer)
			for storage, template in \
				(missiles, client_missile_destroy_template),\
				(alive_players, client_player_bye_template),\
				(dead_players, client_player_bye_template):
				if id in storage:
					value = storage.pop(id)
					bye.append(template.format(missile=value, player=value).encode('utf-8'))

	if bye:
		for writer in alive_connections:
			writer.writelines(bye)


@battle_ensure_consistency
def battle_transfer(moved, alive_players, width, height, connections, alive_connections):
	for intention in moved:
		id, _, direction = intention
		if id not in alive_players:
			continue

		player = alive_players[id]
		delta = STEP_DELTA[direction]
		x = player.x + delta.x
		if x < 0 or x >= width:
			message = client_player_position_template.format(player=player).encode('utf-8')
			writer = connections[id][1]
			writer.write(message)
			continue

		y = player.y + delta.y
		if y < 0 or y >= height:
			message = client_player_position_template.format(player=player).encode('utf-8')
			writer = connections[id][1]
			writer.write(message)
			continue

		player = player._replace(x=x, y=y, direction=direction)
		alive_players[id] = player

		message = client_player_position_template.format(player=player).encode('utf-8')
		for writer in alive_connections:
			writer.write(message)


@battle_ensure_consistency
def battle_push(moment, missiles, width, height, alive_connections):
	for missile in missiles.copy().values():
		gap = moment - missile.future_moment
		if gap < 0:
			continue

		delta = STEP_DELTA[missile.direction]
		missile = missile._replace(
			x=missile.x+delta.x, y=missile.y+delta.y,
			future_moment=moment+MISSILE_VELOCITY_S-gap,
		)
		if \
			missile.x < 0 or missile.x >= width or\
			missile.y < 0 or missile.y >= height:

			del missiles[missile.id]

			message = client_missile_destroy_template.format(missile=missile).encode('utf-8')
			for writer in alive_connections:
				writer.write(message)
			continue

		missiles[missile.id] = missile
		message = client_missile_position_template.format(missile=missile).encode('utf-8')
		for writer in alive_connections:
			writer.write(message)


@battle_ensure_consistency
def battle_blow_up(moment, missiles, alive_players, dead_players, alive_connections):
	for missile in missiles.copy().values():
		exploded = []
		for victim in alive_players.copy().values():
			if victim.id == missile.id:
				continue

			if victim.x == missile.x and victim.y == missile.y:
				if missile.id not in exploded:
					exploded.append(missile.id)
					del missiles[missile.id]
					message_missile_destoy = client_missile_destroy_template.format(missile=missile).encode('utf-8')

				del alive_players[victim.id]
				dead_players[victim.id] = victim._replace(future_moment=moment+DEATH_DURATION_S)
				message_player_die = client_player_die_template.format(player=victim).encode('utf-8')

				storage = alive_players if missile.id in alive_players else dead_players
				missile_owner = storage[missile.id]
				storage[missile_owner.id] = missile_owner._replace(score=missile_owner.score+1)
				message_player_score = client_player_score_template.format(player=missile_owner).encode('utf-8')

				message = (message_missile_destoy, message_player_die, message_player_score)
				for writer in alive_connections:
					writer.writelines(message)


@battle_ensure_consistency
def battle_pull_the_trigger(moment, fired, missiles, alive_players, dead_players, alive_connections):
	shoot = False
	for intention in fired:
		id, *_ = intention
		if id in dead_players or id in missiles:
			continue

		shoot = True

		player = alive_players[id]
		missile = Missile(
			id=player.id, x=player.x, y=player.y, direction=player.direction,
			future_moment=moment+MISSILE_VELOCITY_S,
		)
		missiles[id] = missile

		message = client_missile_position_template.format(missile=missile).encode('utf-8')
		for writer in alive_connections:
			writer.write(message)

	return shoot


def battle_recognize(joined, width, height, spawn_delta, alive_players, missiles, dead_players, connections, alive_connections):
	new = []
	for intention in joined:
		id, _, color = intention
		x, y = battle_find_spawn_point(width, height, spawn_delta, alive_players, missiles)
		new_writer = connections[id][1]
		new_player = Player(id, color, x, y, Direction(0), 0, None)
		alive_players[id] = new_player

		for player in alive_players.values():
			message = client_joined_template.format(player=player).encode('utf-8')
			new_writer.write(message)
		for player in dead_players.values():
			message = client_joined_template.format(player=player).encode('utf-8')
			new_writer.write(message)
			message = client_player_die_template.format(player=player).encode('utf-8')
			new_writer.write(message)
		for missile in missiles.values():
			message = client_missile_position_template.format(missile=missile).encode('utf-8')
			new_writer.write(message)

		message = client_id_template.format(player=new_player).encode('utf-8')
		new_writer.write(message)

		message = client_joined_template.format(player=new_player).encode('utf-8')
		for writer in chain(alive_connections, new):
			writer.write(message)

		new.append(new_writer)

	return new


@battle_ensure_consistency
def battle_spawn(moment, dead_players, width, height, spawn_delta, alive_players, missiles, alive_connections):
	for player in dead_players.copy().values():
		if moment < player.future_moment:
			continue

		del dead_players[player.id]
		x, y = battle_find_spawn_point(width, height, spawn_delta, alive_players, missiles)
		player = player._replace(x=x, y=y, future_moment=None)
		alive_players[player.id] = player

		message = client_player_position_template.replace('\n', ' :spawn \n').format(player=player).encode('utf-8')
		for writer in alive_connections:
			writer.write(message)


@battle_ensure_consistency
def battle_find_spawn_point(width, height, spawn_delta, alive_players, missiles):
	"""
	Return random point on battlefield, try easy to avoid players and missiles
	"""
	x = randrange(0, width)
	y = randrange(0, height)
	holded_points = chain(
		((player.x, player.y) for player in alive_players.values()),
		((missile.x, missile.y) for missile in missiles.values()),
	)
	for holded_x, holded_y in sorted(holded_points, key=lambda v: v[0]+v[1]*height):
		if holded_x == x and holded_y == y:
			x += randint(1, spawn_delta)
			y += randint(1, spawn_delta) * choice((-1, 1))
			x = min(0, x, width-1)
			y = min(0, abs(y), height-1)

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
	if DEBUG:
		logging.basicConfig(level=logging.DEBUG)
	loop = asyncio.get_event_loop()
	loop.set_debug(enabled=DEBUG)
	for signame in ('SIGINT', 'SIGTERM'):
		loop.add_signal_handler(getattr(signal, signame), functools.partial(ask_exit, loop, signame))
	try:
		asyncio.ensure_future(manage_battle())
		loop.run_forever()
	finally:
		loop.close()


if __name__ == '__main__':
	main()
