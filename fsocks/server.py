#!/usr/bin/env python3
import socket
import select
from threading import Thread
from fsocks import logger, config
from fsocks.net import send_all
from fsocks.socks import CMD, VERSION, ATYPE, REP,\
        Message, ProxyError
from fsocks.cipher.xor import XOR


def handle_conn(clientfd):
    cipher = XOR(0x26)
    encrypt = cipher.encrypt
    decrypt = cipher.decrypt
    try:
        req = Message.from_sock(clientfd, wrapper=decrypt)
    except ProxyError as e:
        logger.warn(e)
        clientfd.close()
        return
    logger.info(req)
    if req.msg is CMD.CONNECT:
        remotefd = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        logger.info('connecting to {}:{}'.format(req.addr[0], req.addr[1]))
        try:
            remotefd.connect(req.addr)
        except (ConnectionRefusedError, TimeoutError) as e:
            logger.warn('{}:{} {}'.format(req.addr[0], req.addr[1], e))
            return
        bind_address = remotefd.getsockname()
        reply = Message(
                ver=VERSION.SOCKS5,
                msg=REP.SUCCEEDED,
                atype=ATYPE.IPV4,
                addr=bind_address)
        reply.to_sock(clientfd, wrapper=encrypt)
        # now forwarding data
        rdset = [clientfd, remotefd]
        while True:
            rlist, _, _ = select.select(rdset, [], [])
            if clientfd in rlist:
                try:
                    data = clientfd.recv(4096)
                except ConnectionResetError as e:
                    logger.warn(str(e))
                    break
                if len(data) == 0:
                    break
                send_all(remotefd, decrypt(data))
            if remotefd in rlist:
                data = remotefd.recv(4096)
                if len(data) == 0:
                    break
                send_all(clientfd, encrypt(data))
        remotefd.close()
        clientfd.close()
    else:
        logger.error('not handled')
    logger.info('handle done')


def main():
    config.load_args()
    serverfd = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    serverfd.bind(config.server_address)
    serverfd.listen(5)
    logger.info('Server started on {}:{}'.format(
        config.server_host, config.server_port))
    while True:
        clientfd, addr = serverfd.accept()
        # no greetings, handle CONNECT command
        t = Thread(target=handle_conn, args=(clientfd,))
        t.setDaemon(True)
        t.start()


if __name__ == '__main__':
    main()