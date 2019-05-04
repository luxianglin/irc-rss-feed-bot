import logging
import os
import queue
import random
import subprocess
import threading
import time
from typing import Callable, Dict, List, Tuple

import bitlyshortener
import miniirc

from . import config
from .db import Database
from .feed import Feed
from .util.datetime import timedelta_desc

log = logging.getLogger(__name__)


def _alert(irc: miniirc.IRC, msg: str, logger: Callable[[str], None] = log.exception) -> None:
    logger(msg)
    irc.msg(config.INSTANCE['alerts_channel'], msg)


class Bot:
    CHANNEL_JOIN_EVENTS: Dict[str, threading.Event] = {}
    CHANNEL_LAST_INCOMING_MSG_TIMES: Dict[str, float] = {}
    CHANNEL_QUEUES: Dict[str, queue.Queue] = {}
    FEED_GROUP_BARRIERS: Dict[str, threading.Barrier] = {}

    def __init__(self) -> None:
        log.info('Initializing bot as: %s', subprocess.check_output('id', text=True).rstrip())
        instance = config.INSTANCE
        self._outgoing_msg_lock = threading.Lock()  # Used for rate limiting across multiple channels.
        self._db = Database()
        self._url_shortener = bitlyshortener.Shortener(
            tokens=[token.strip() for token in os.environ['BITLY_TOKENS'].strip().split(',')],
            max_cache_size=config.BITLY_SHORTENER_MAX_CACHE_SIZE)

        # Setup miniirc
        log.debug('Initializing IRC client.')
        self._irc = miniirc.IRC(
            ip=instance['host'],
            port=instance['ssl_port'],
            nick=instance['nick'],
            channels=instance['feeds'],
            ssl=True,
            debug=False,
            ns_identity=(instance['nick'], os.environ['IRC_PASSWORD']),
            connect_modes=instance.get('mode'),
            quit_message='',
            ping_interval=30,
            )
        log.info('Initialized IRC client.')

        self._setup_channels()
        log.info('Alerts will be sent to %s.', instance['alerts_channel'])

    def _msg_channel(self, channel: str) -> None:
        log.debug('Channel messenger for %s is starting and is waiting to be notified of channel join.', channel)
        instance = config.INSTANCE
        channel_queue = Bot.CHANNEL_QUEUES[channel]
        db = self._db
        irc = self._irc
        message_format = config.MESSAGE_FORMAT
        min_channel_idle_time = config.MIN_CHANNEL_IDLE_TIME
        seconds_per_msg = config.SECONDS_PER_MESSAGE
        Bot.CHANNEL_JOIN_EVENTS[channel].wait()
        Bot.CHANNEL_JOIN_EVENTS[instance['alerts_channel']].wait()
        log.info('Channel messenger for %s has started.', channel)
        while True:
            feed = channel_queue.get()
            log.debug('Dequeued %s.', feed)
            try:
                if feed.postable_entries:  # Result gets cached.
                    try:
                        while True:
                            if not self._outgoing_msg_lock.acquire(blocking=False):
                                log.info('Waiting to acquire outgoing message lock to post %s.', feed)
                                self._outgoing_msg_lock.acquire()
                            last_incoming_msg_time = Bot.CHANNEL_LAST_INCOMING_MSG_TIMES[channel]
                            time_elapsed_since_last_ic_msg = time.monotonic() - last_incoming_msg_time
                            sleep_time = max(0, min_channel_idle_time - time_elapsed_since_last_ic_msg)
                            if sleep_time == 0:
                                break  # Lock will be released later after posting messages.
                            self._outgoing_msg_lock.release()  # Releasing lock before sleeping.
                            log.info('Will wait %s for channel inactivity to post %s.',
                                     timedelta_desc(sleep_time), feed)
                            time.sleep(sleep_time)

                        log.debug('Checking IRC client connection state.')
                        if not irc.connected:  # In case of netsplit.
                            log.warning('Will wait for IRC client to connect so as to post %s.', feed)
                            disconnect_time = time.monotonic()
                            while not irc.connected:
                                time.sleep(5)
                            disconnection_time = time.monotonic() - disconnect_time
                            log.info('IRC client is connected after waiting %s.',
                                     timedelta_desc(disconnection_time))

                        log.info('Posting %s entries for %s.', len(feed.postable_entries), feed)
                        for entry in feed.postable_entries:
                            msg = message_format.format(feed=feed.name, title=entry.title, url=entry.post_url)
                            outgoing_msg_time = time.monotonic()
                            irc.msg(channel, msg)
                            log.debug('Sent message to %s: %s', channel, msg)
                            time.sleep(max(0., outgoing_msg_time + seconds_per_msg - time.monotonic()))
                    finally:
                        self._outgoing_msg_lock.release()
                    log.info('Posted %s entries for %s.', len(feed.postable_entries), feed)

                if feed.unposted_entries:  # Note: feed.postable_entries is intentionally not used here.
                    db.insert_posted(channel, feed.name, [entry.long_url for entry in feed.unposted_entries])

            except Exception as exc:
                msg = f'Error processing {feed}: {exc}'
                _alert(irc, msg)
            channel_queue.task_done()

    def _read_feed(self, channel: str, feed_name: str) -> None:
        log.debug('Feed reader for feed %s of %s is starting and is waiting to be notified of channel join.',
                  feed_name, channel)
        instance = config.INSTANCE
        feed_config = instance['feeds'][channel][feed_name]
        channel_queue = Bot.CHANNEL_QUEUES[channel]
        feed_url = feed_config['url']
        feed_period_avg = max(config.PERIOD_HOURS_MIN, feed_config.get('period', config.PERIOD_HOURS_DEFAULT)) * 3600
        feed_period_min = feed_period_avg * (1 - config.PERIOD_RANDOM_PERCENT / 100)
        feed_period_max = feed_period_avg * (1 + config.PERIOD_RANDOM_PERCENT / 100)
        irc = self._irc
        db = self._db
        url_shortener = self._url_shortener
        query_time = time.monotonic() - (feed_period_avg / 2)  # Delays first read by half of feed period.
        Bot.CHANNEL_JOIN_EVENTS[channel].wait()  # Optional.
        Bot.CHANNEL_JOIN_EVENTS[instance['alerts_channel']].wait()
        log.debug('Feed reader for feed %s of %s has started.', feed_name, channel)
        while True:
            feed_period = random.uniform(feed_period_min, feed_period_max)
            query_time = max(time.monotonic(), query_time + feed_period)  # "max" is used in case of wait using "put".
            sleep_time = max(0., query_time - time.monotonic())
            if sleep_time != 0:
                log.debug('Will wait %s to read feed %s of %s.', timedelta_desc(sleep_time), feed_name, channel)
                time.sleep(sleep_time)

            try:
                # Read feed
                log.debug('Retrieving feed %s of %s.', feed_name, channel)
                feed = Feed(channel=channel, name=feed_name, url=feed_url, db=db, url_shortener=url_shortener)
                log.info('Retrieved %s with %s approved entries.', feed, len(feed.entries))

                # Wait for other feeds in group
                if feed_config.get('group'):
                    feed_group = feed_config['group']
                    group_barrier = Bot.FEED_GROUP_BARRIERS[feed_group]
                    num_other = group_barrier.parties - 1
                    num_pending = num_other - group_barrier.n_waiting
                    if num_pending > 0:  # This is not thread-safe but that's okay for logging.
                        log.debug('Will wait for %s of %s other feeds in group %s to also be read before queuing %s.',
                                  num_pending, num_other, feed_group, feed)
                    group_barrier.wait()
                    log.debug('Finished waiting for other feeds in group %s to be read before queuing %s.',
                              feed_group, feed)

                # Queue feed
                try:
                    channel_queue.put_nowait(feed)
                except queue.Full:
                    feed_desc = str(feed).capitalize()
                    msg = f'Queue for {channel} is full. {feed_desc} will be put in the queue in blocking mode.'
                    _alert(irc, msg, log.warning)
                    channel_queue.put(feed)
                else:
                    log.debug('Queued %s.', feed)
            except Exception as exc:
                _alert(irc, f'Error reading feed {feed_name} of {channel}: {exc}')
            else:
                if instance.get('once'):
                    log.warning('Discontinuing reader for %s.', feed)
                    return
                del feed

    def _setup_channels(self) -> None:
        instance = config.INSTANCE
        channels = instance['feeds']
        channels_str = ', '.join(channels)
        log.debug('Setting up threads and queues for %s channels (%s) and their feeds with %s currently active '
                  'threads.', len(channels), channels_str, threading.active_count())
        num_feeds_setup = 0
        barriers_parties: Dict[str, int] = {}
        for channel, channel_config in channels.items():
            log.debug('Setting up threads and queue for %s.', channel)
            num_channel_feeds = len(channel_config)
            self.CHANNEL_JOIN_EVENTS[channel] = threading.Event()
            self.CHANNEL_QUEUES[channel] = queue.Queue(maxsize=num_channel_feeds * 2)
            threading.Thread(target=self._msg_channel, name=f'ChannelMessenger-{channel}',
                             args=(channel,)).start()
            for feed, feed_config in channel_config.items():
                threading.Thread(target=self._read_feed, name=f'FeedReader-{channel}-{feed}',
                                 args=(channel, feed)).start()
                num_feeds_setup += 1
                if feed_config.get('group'):
                    group = feed_config['group']
                    barriers_parties[group] = barriers_parties.get(group, 0) + 1
            log.debug('Finished setting up threads and queue for %s and its %s feeds with %s currently active threads.',
                      channel, num_channel_feeds, threading.active_count())
        for barrier, parties in barriers_parties.items():
            self.FEED_GROUP_BARRIERS[barrier] = threading.Barrier(parties)
        log.info('Finished setting up %s channels (%s) and their %s feeds with %s currently active threads.',
                 len(channels), channels_str, num_feeds_setup, threading.active_count())

# Ref: https://tools.ietf.org/html/rfc1459


@miniirc.Handler('JOIN')
def _handle_join(_irc: miniirc.IRC, hostmask: Tuple[str, str, str], args: List[str]) -> None:
    # Parse message
    log.debug('Handling channel join: hostmask=%s, args=%s', hostmask, args)
    user, ident, hostname = hostmask
    channel = args[0]

    # Ignore if not actionable
    if (user != config.INSTANCE['nick']) or (channel.casefold() not in config.INSTANCE['channels:casefold']):
        return

    # Update channel last message time
    Bot.CHANNEL_JOIN_EVENTS[channel].set()
    Bot.CHANNEL_LAST_INCOMING_MSG_TIMES[channel] = time.monotonic()
    log.debug('Set the last incoming message time for %s to %s.',
              channel, Bot.CHANNEL_LAST_INCOMING_MSG_TIMES[channel])


@miniirc.Handler('PRIVMSG')
def _handle_privmsg(irc: miniirc.IRC, hostmask: Tuple[str, str, str], args: List[str]) -> None:
    # Parse message
    log.debug('Handling incoming message: hostmask=%s, args=%s', hostmask, args)
    channel = args[0]

    # Ignore if not actionable
    if channel.casefold() not in config.INSTANCE['channels:casefold']:
        assert channel.casefold() == config.INSTANCE['nick:casefold']
        user, ident, hostname = hostmask
        msg = args[-1]
        assert msg.startswith(':')
        msg = msg[1:]
        if msg != '\x01VERSION\x01':
            # Ignoring private message from freenode-connect having ident frigg
            # and hostname freenode/utility-bot/frigg: VERSION
            _alert(irc, f'Ignoring private message from {user} having ident {ident} and hostname {hostname}: {msg}',
                   log.warning)
        return

    # Update channel last message time
    Bot.CHANNEL_LAST_INCOMING_MSG_TIMES[channel] = time.monotonic()
    log.debug('Updated the last incoming message time for %s to %s.',
              channel, Bot.CHANNEL_LAST_INCOMING_MSG_TIMES[channel])
