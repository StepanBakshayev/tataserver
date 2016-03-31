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