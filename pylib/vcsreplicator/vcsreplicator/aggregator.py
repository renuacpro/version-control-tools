# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

from __future__ import absolute_import, unicode_literals

import logging
import sys
import time

from .config import Config
from .consumer import Consumer
from .daemon import (
    run_in_loop,
)
from .producer import Producer
from .util import consumer_offsets


logger = logging.getLogger('vcsreplicator.aggregator')


def read_consumer_groups(path):
    consumer_groups = []
    with open(path, 'rb') as fh:
        for line in fh:
            line = line.strip()
            if line:
                consumer_groups.append(line)

    return consumer_groups


def _run_aggregation(client, consumer_topic, consumer_groups_path, ack_group,
                    producer_topic, alive, poll_interval=1.0):
    # We read the consumer groups file on every iteration so the set of
    # consumers can be dynamic. This allows consumers to be marked as
    # offline without causing a stall in message copying.
    consumer_groups = read_consumer_groups(consumer_groups_path)

    count = synchronize_fully_consumed_messages(
        client=client,
        consumer_topic=consumer_topic,
        consumer_groups=consumer_groups,
        ack_group=ack_group,
        producer_topic=producer_topic,
        alive=alive)

    if not alive[0]:
        return

    # If we didn't have any unacked messages, we don't want to busy
    # loop polling for offsets. So add a delay. Ideally we would wait on
    # a message to arrive and react to that instantly. For now, a small
    # polling interval should be sufficient.
    if not count:
        time.sleep(poll_interval)


def synchronize_fully_consumed_messages(client, consumer_topic, consumer_groups,
                                        ack_group, producer_topic, alive):
    """Replay messages of a monitored topic when all consumers have acked them.

    Given a consumer topic, query the consumer offsets for the groups specified
    and copy messages that have been consumed by all consumers into another
    topic.

    Returns the number of unacked messages that were present when the function
    was called.

    This function should be called repeatedly to ensure the destination topic
    remains up to date.
    """
    groups = [ack_group] + consumer_groups
    offsets = consumer_offsets(client, consumer_topic, groups)

    acked_offsets = offsets['group'][ack_group]

    fully_consumed_offsets = {}
    for partition in offsets['available']:
        consumed_offsets = [offsets['group'][g][partition]
                            for g in consumer_groups]

        # The fully consumed offset is defined as the offset that all consumer
        # groups have acknowledged.
        fully_consumed_offsets[partition] = min(consumed_offsets)

    unacked_partition_counts = {}  # partition -> count
    for partition, consumed_offset in sorted(fully_consumed_offsets.items()):
        acked_offset = acked_offsets[partition]
        if consumed_offset > acked_offset:
            unacked_partition_counts[partition] = consumed_offset - acked_offset

    # All fully consumed messages have been written to ack topic.
    if not unacked_partition_counts:
        return 0

    unacked_count = sum(unacked_partition_counts.values())
    logger.warn('%d unacked messages in %d partition: [%s]' % (
                unacked_count, len(unacked_partition_counts),
                ', '.join(map(str, sorted(unacked_partition_counts.keys())))))

    copy_messages(client=client,
                  consumer_topic=consumer_topic,
                  consumer_group=ack_group,
                  # Mutated by function call, so make a copy.
                  counts=dict(unacked_partition_counts),
                  producer_topic=producer_topic,
                  alive=alive)

    return unacked_count


def copy_messages(client, consumer_topic, consumer_group, counts,
                  producer_topic, alive):
    """Record all unacked messages in another topic.

    Essentially, this copies messages from one topic to another.

    The ``counts`` argument is a dict of partition to count of messages to
    copy.
    """

    consumer = Consumer(client, consumer_group, consumer_topic, counts.keys())
    producer = Producer(client, producer_topic, batch_send=False,
                        req_acks=-1, ack_timeout=30000)

    # Our strategy is to retrieve messages from the partitions that we need
    # to copy from. When the count of unacked messages in a partition reaches
    # 0, we stop consuming from the partition.
    while counts and alive[0]:
        r = consumer.get_message(timeout=5.0)
        if not r:
            logger.warn('unacked messages but retrieving message failed; weird')
            continue

        partition, message, payload = r

        # The Kafka consumer may fetch multiple messages and leave them in
        # a buffer/queue. Below, we tell the consumer to stop fetching
        # messages from certain partitions. However, it may have already fetched
        # a message from a partition we are done with and will serve it up from
        # its buffer/queue. If we get one of these messages, ignore it.
        if partition not in counts:
            logger.warn('got message from exhausted partition %d; ignoring' % partition)
            continue

        logger.warn('copying %s from partition %d' % (payload['name'], partition))

        payload['_original_created'] = payload['_created']
        payload['_original_partition'] = partition

        # It isn't possible to atomically write into the producer and
        # acknowledge the consumer offset. So, there is a chance the produce
        # could succeed and the offset commit could fail. This would result
        # in duplicate messages in the aggregate topic. This is a lesser
        # evil than losing data. So always send before committing.
        producer.send_message(payload, 0)

        consumer.commit(partitions=[partition])

        # Now decrement the unacked message count for this partition and stop
        # consuming this partiion if no more unacked messages.
        counts[partition] -= 1
        if counts[partition] == 0:
            del counts[partition]
            del consumer.offsets[partition]
            del consumer.fetch_offsets[partition]


def cli():
    """Command line interface to run the aggregator."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('config', help='Path to config file to load')
    parser.add_argument('--onetime', action='store_true',
                        help='Run once instead of forever')
    args = parser.parse_args()

    config = Config(filename=args.config)
    client = config.get_client_from_section('aggregator', timeout=5)
    topic = config.c.get('aggregator', 'monitor_topic')
    groups_path = config.c.get('aggregator', 'monitor_groups_file')
    ack_group = config.c.get('aggregator', 'ack_group')
    aggregate_topic = config.c.get('aggregator', 'aggregate_topic')

    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(name)s %(message)s')
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    root.addHandler(handler)

    try:
        run_in_loop(logger, _run_aggregation, onetime=args.onetime,
                    client=client,
                    consumer_topic=topic,
                    consumer_groups_path=groups_path,
                    ack_group=ack_group,
                    producer_topic=aggregate_topic)
    except BaseException:
        logger.error('exiting main consume loop with error')
        raise
