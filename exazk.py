#!/usr/bin/env python

import yaml
import time
import sys
import signal
import os
import logging
import subprocess

from kazoo.client import KazooClient, KazooState
from kazoo.exceptions import SessionExpiredError

logging.basicConfig()
logger = logging.getLogger()
logger.setLevel('DEBUG')

class Alarm(Exception):
    pass
def alarm_signal_handler (number, frame):  # pylint: disable=W0613
    raise Alarm()

class ServiceChecker:
    def __init__(self, command):
        self.command = command

    def check(self):
        try:
            signal.signal(signal.SIGALRM, alarm_signal_handler)
            signal.alarm(1)

            DEVNULL = open(os.devnull, 'w')
            p = subprocess.Popen(self.command, shell=True,
                    stdout=DEVNULL, stderr=DEVNULL, close_fds=True,
                    preexec_fn=os.setpgrp)

            rc = p.wait()
            signal.signal(signal.SIGALRM, signal.SIG_IGN)
            if rc == 0:
                return True
            else:
                logger.error('local check returned: %s' % rc)

        except Alarm:
            logger.error("local check spent more than 1s to run")
            os.killpg(p.pid, signal.SIGKILL)

        return False

class BGPTable:
    def __init__(self):
        self.announce = []

    def add_route(self, **route):
        if 'prefix' not in route or 'dst' not in route or 'metric' not in route:
            raise Exception('prefix, dst & metric are mandatory in route')
        logger.debug('adding BGP route: %s' % route['prefix'])
        self.announce.append(route)

    def get_routes(self):
        return self.announce

class BGPSpeaker:

    def __init__(self, table):
        if not isinstance(table, BGPTable):
            raise Exception('BGPTable object expected')

        self.table = table

    def advertise_routes(self):
        logger.info("advertising routes")
        announce = self.table.get_routes()
        for route in announce:
            print('announce route %s/32 next-hop self med %s' %
                (route['prefix'], route['metric']))
        sys.stdout.flush()

    def withdraw_route(self, **route):
        if 'prefix' not in route or 'dst' not in route or 'metric' not in route:
            raise Exception('prefix, dst & metric are mandatory in route')
        logger.debug('removing BGP route: %s' % route['prefix'])
        print('withdraw route %s/32 next-hop self med %s' %
            (route['prefix'], route['metric']))
        sys.stdout.flush()

class EZKConfFactory:
    def create_from_yaml_file(self, path):
        yml = yaml.safe_load(open(path))

        return EZKConf(**yml)

class EZKConf:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

class EZKRuntime:
    def __init__(self, conf, zk):
        if not isinstance(conf, EZKConf):
            raise Exception('EZKConf object expected')

        if not isinstance(zk, KazooClient):
            raise Exception('KazooClient expected')

        self.conf = conf
        self.zk = zk
        self.bgp_table = BGPTable()

        # flags
        self.refresh = True
        self.recreate = True
        self.shouldstop = False

        # slow & fast cycles
        self.longsleep = 10
        self.shortsleep = 0.1

    def set_bgp_table(self, table):
        if not isinstance(table, BGPTable):
            raise Exception('BGPTable object expected')

        self.bgp_table = table

    def get_bgp_table(self):
        return self.bgp_table

    def get_conf(self):
        return self.conf

    def get_zk(self):
        return self.zk

    def create_node(self):
        self.recreate = False
        logger.info('re-creating my ephemeral node')

        try:
            self.get_zk().create('%s/%s/%s' % (
                self.get_conf().zk_path_service,
                self.get_conf().srv_name,
                self.get_conf().srv_auth_ip), ephemeral=True)
        except SessionExpiredError as e:
            self.recreate = True

    def refresh_children(self):
        self.refresh = False
        logger.info('refreshing children & routes')

        children = self.get_zk().get_children('%s/%s' % (
                self.get_conf().zk_path_service,
                self.get_conf().srv_name))
        bgp_table = BGPTable()

        for ip in self.get_conf().srv_non_auth_ips:
            if ip not in children:
                bgp_table.add_route(prefix=ip, dst='1.1.1.1', metric=200)
            else:
                BGPSpeaker(BGPTable()).withdraw_route(prefix=ip, dst='1.1.1.1', metric=200)

        bgp_table.add_route(prefix=runtime.get_conf().srv_auth_ip,
                dst='1.1.1.1', metric=100)
        self.set_bgp_table(bgp_table)

    def trigger_refresh(self):
        self.refresh = True

    def trigger_recreate(self):
        self.recreate = True

class EZKState(KazooState):
    INIT = "INIT"

logger.info('ExaZK starting...')
conf = EZKConfFactory().create_from_yaml_file(sys.argv[1])
zk = KazooClient(hosts=','.join(conf.zk_hosts))
runtime = EZKRuntime(conf=conf, zk=zk)

# exits gracefully when possible
def exit_signal_handler(signal, frame):
    logger.info('received signal %s, preparing to stop' % signal)
    runtime.shouldstop = True

signal.signal(signal.SIGINT, exit_signal_handler)
signal.signal(signal.SIGTERM, exit_signal_handler)

def zk_transition(state):
    logger.info('zk state changed to %s' % state)

    if state == KazooState.SUSPENDED:
        logger.error('zk disconnected, flushing routes...')

        runtime.set_bgp_table(BGPTable())
        bgp_speaker = BGPSpeaker(BGPTable())
        for ip in runtime.get_conf().srv_non_auth_ips:
            bgp_speaker.withdraw_route(prefix=ip, dst='1.1.1.1', metric=200)
        bgp_speaker.withdraw_route(prefix=runtime.get_conf().srv_auth_ip,
            dst='1.1.1.1', metric=100)

    if state == KazooState.LOST:
        logger.error('zk lost, have to re-create ephemeral node')
        runtime.trigger_recreate()

    if state == KazooState.CONNECTED:
        runtime.trigger_refresh()

runtime.get_zk().add_listener(zk_transition)
runtime.get_zk().start()
runtime.get_zk().ensure_path('%s/%s' % (
    runtime.get_conf().zk_path_service,
    runtime.get_conf().srv_name))

while runtime.get_zk().exists('%s/%s/%s' % (
    runtime.get_conf().zk_path_service,
    runtime.get_conf().srv_name,
    runtime.get_conf().srv_auth_ip)):
    logger.warn('stale node found, sleeping(1)...')
    time.sleep(1)

@zk.ChildrenWatch('%s/%s' % (
    runtime.get_conf().zk_path_service,
    runtime.get_conf().srv_name))
def zk_watch(children):
    logger.debug('zk children are %s' % children)
    runtime.trigger_refresh()

while not runtime.shouldstop:

    now = start = time.time()
    while not runtime.refresh and not runtime.recreate \
            and now<start+runtime.longsleep \
            and not runtime.shouldstop:

        time.sleep(runtime.shortsleep)
        now = time.time()

    if runtime.shouldstop:
        break

    if not ServiceChecker(runtime.get_conf().local_check).check():
        continue

    if runtime.recreate:
        runtime.create_node()

    if runtime.refresh:
        runtime.refresh_children()

    BGPSpeaker(runtime.get_bgp_table()).advertise_routes()

# main loop exited, cleaning resources
try:
    runtime.get_zk().stop()
    runtime.get_zk().close()
    logger.info('ExaZK stopped')
except Exception as e:
    logger.error('did my best but something went wrong while stopping :(')
