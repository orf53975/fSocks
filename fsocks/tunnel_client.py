#!/usr/bin/env python3
import sys
import asyncio
from fsocks import logger, config, protocol, socks
from fsocks import fuzzing, cryption


class User:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.user_id = writer.transport._sock_fd
        self.remote_id = None
        self.task = None

    @property
    def actived(self):
        return self.task is not None

    @property
    def established(self):
        return self.remote_id is not None

    def close(self):
        self.remote_id = None
        self.writer.transport.abort()
        # self.task.cancel()
        # self.task = None

    def __str__(self):
        return 'User(%d)' % self.user_id


class TunnelClient:
    """
    fSocks tunnel client, and SOCK5 server for user
    """

    def __init__(self):
        self.socks_server = None
        self.users = {}  # user_id -> User
        # Tunnel client
        # TODO: one tunnel client may have many tunnels
        self.tunnel_task = None
        self.tunnel_reader = None
        self.tunnel_writer = None
        self.cipher = cryption.AES256CBC(config.password)
        self.fuzz = None

    def _accept_user(self, user_reader, user_writer):
        logger.debug('user accepted')
        user = User(user_reader, user_writer)
        task = asyncio.Task(self._handle_user(user))
        user.task = task
        self.users[user.user_id] = user

        def user_done(task):
            logger.debug('user task done')
        task.add_done_callback(user_done)

    def _user_closed(self, user):
        logger.debug('{} closed'.format(user))
        if user.established:
            self.tunnel_writer.write(
                protocol.Close(user.user_id).to_packet(self.fuzz))
        user.writer.transport.abort()

    def _delete_user(self, user):
        user.close()
        if user.user_id in self.users:
            del self.users[user.user_id]

    def _get_user(self, user_id):
        return self.users.get(user_id, None)

    async def safe_write(self, writer, data):
        writer.write(data)
        try:
            await writer.drain()
        except ConnectionResetError as e:
            logger.warn('write error: {}'.format(e))

    async def _pipe_user(self, user):
        # may start before connection to remote is established
        while True:
            try:
                data = await user.reader.read(2048)
            except ConnectionResetError:
                logger.warn('user connection reset')
                data = b''
            if len(data) == 0:
                self._user_closed(user)
                break
            assert user.established
            packet = protocol.Relaying(
                user.user_id, user.remote_id, data)
            await self.safe_write(self.tunnel_writer,
                                  packet.to_packet(self.fuzz))

    async def _handle_user(self, user):
        # ignore client SOCKS5 greeting
        data = await user.reader.read(256)
        logger.debug('ignore SOCK5 greeting ({} bytes)'.format(len(data)))
        # response greeting without auth
        server_greeting = socks.ServerGreeting()
        await self.safe_write(user.writer,
                              server_greeting.to_bytes())
        # recv CMD
        try:
            msg = await socks.Message.from_reader(user.reader)
        except asyncio.streams.IncompleteReadError:
            self._delete_user(user)
            return
        if msg.code is not socks.CMD.CONNECT:
            logger.warn('unhandle msg {}'.format(msg))
            rep = socks.Message(
                socks.VER.SOCKS5,
                socks.REP.COMMAND_NOT_SUPPORTED,
                socks.ATYPE.IPV4,
                ('0', 0))
            await self.safe_write(user.writer, rep.to_bytes())
            return
        logger.info('connecting {}:{}'.format(msg.addr[0], msg.addr[1]))
        # send to tunnel
        connect_reqeust = protocol.Request(
            user.user_id, 0, msg)
        await self.safe_write(self.tunnel_writer,
                              connect_reqeust.to_packet(self.fuzz))
        await self._pipe_user(user)

    async def _handle_tunnel(self, reader, writer):
        logger.debug('_handle_tunnel started')
        while True:
            packet = await protocol.async_read_packet(reader, self.fuzz)
            if packet.mtype is protocol.MTYPE.REPLY:
                # received a SOCKS reply, update mapping
                # and forward to corresponding user
                remote_id = packet.src
                user_id = packet.dst
                user = self._get_user(user_id)
                if user is None:
                    # Tell server to close
                    continue
                await self.safe_write(user.writer,
                                      packet.msg.to_bytes())
                user.remote_id = remote_id
            elif packet.mtype is protocol.MTYPE.RELAYING:
                # received raw data, forwarding
                remote_id = packet.src
                user_id = packet.dst
                user = self._get_user(user_id)
                if user is None:
                    # Tell server to close
                    continue
                await self.safe_write(user.writer, packet.payload)
            elif packet.mtype is protocol.MTYPE.CLOSE:
                # close user tansport
                user_id = packet.src
                logger.debug(
                    'remote disconnected, close user {}'.format(user_id))
                user = self._get_user(user_id)
                if user is None:
                    # ignore
                    continue
                self._delete_user(user)
            else:
                logger.warn('unknown packet {}'.format(packet))
        logger.debug('_handle_tunnel exited')

    async def start_tunnel(self, loop, host, port):
        logger.info('negotiate with server {}:{}'.format(
            config.server_host, config.server_port))
        reader, writer = await asyncio.open_connection(host, port)
        # > Hello
        hello_request = protocol.Hello()
        await self.safe_write(writer, hello_request.to_packet(self.cipher))
        # < Hello
        hello_response = await protocol.async_read_packet(reader, self.cipher)
        logger.debug(hello_response)
        # > HandShake
        shake_request = protocol.HandShake(timestamp=hello_response.timestamp)
        await self.safe_write(writer, shake_request.to_packet(self.cipher))
        # < HandShake
        shake_response = await protocol.async_read_packet(reader, self.cipher)
        logger.debug(shake_response)
        logger.info('negotiate done, using fuzz: {}'.format(
            shake_response.fuzz))
        self.fuzz = shake_response.fuzz
        self.tunnel_reader = reader
        self.tunnel_writer = writer
        self.tunnel_task = asyncio.Task(self._handle_tunnel(reader, writer))

        def tunnel_done(task):
            logger.warn('tunnel is closed')
            sys.exit(2)
        self.tunnel_task.add_done_callback(tunnel_done)
        return True

    def start(self, loop):
        try:
            loop.run_until_complete(
                self.start_tunnel(loop,
                                  config.server_host,
                                  config.server_port))
        except Exception as e:
            logger.error('Negotiate failed: {}'.format(e))
            sys.exit(1)
        self.socks_server = loop.run_until_complete(
            asyncio.streams.start_server(self._accept_user,
                                         config.client_host,
                                         config.client_port,
                                         loop=loop))
        logger.info('SOCKS5 server listen on {}:{}'.format(
            config.client_host, config.client_port))

    def stop(self, loop):
        if self.socks_server is not None:
            self.socks_server.close()
            loop.run_until_complete(self.socks_server.wait_closed())
            self.socks_server = None
        if self.tunnel_task is not None:
            self.tunnel_task.cancel()
        loop.run_until_complete(asyncio.wait(
            [u.task for u in self.users.values() if u.actived] +
            [self.tunnel_task]))


def main():
    config.load_args()
    loop = asyncio.get_event_loop()
    tunnel = TunnelClient()
    tunnel.start(loop)
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        tunnel.stop(loop)
        loop.close()


if __name__ == '__main__':
    main()
