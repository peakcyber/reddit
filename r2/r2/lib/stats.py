import collections
import random
import time

from pycassa import columnfamily
from pycassa import pool

from r2.lib import cache
from r2.lib import utils

class Stats:
    # Sample rate for recording cache hits/misses, relative to the global
    # sample_rate.
    CACHE_SAMPLE_RATE = 0.01

    CASSANDRA_KEY_SUFFIXES = ['error', 'ok']

    def __init__(self, addr, sample_rate):
        if addr:
            import statsd
            self.statsd = statsd
            self.host, port = addr.split(':')
            self.port = int(port)
            self.sample_rate = sample_rate
            self.connection = self.statsd.connection.Connection(
                self.host, self.port, self.sample_rate)
        else:
            self.host = None
            self.port = None
            self.sample_rate = None
            self.connection = None
        self.cassandra_events = collections.defaultdict(int)

    def get_timer(self, name):
        if self.connection:
            return self.statsd.timer.Timer(name, self.connection)
        else:
            return None

    def transact(self, action, service_time_sec):
        timer = self.get_timer('service_time')
        if timer:
            timer.send(action, service_time_sec)

    def get_counter(self, name):
        if self.connection:
            return self.statsd.counter.Counter(name, self.connection)
        else:
            return None

    def action_count(self, counter_name, name, delta=1):
        counter = self.get_counter(counter_name)
        if counter:
            from pylons import request
            counter.increment('%s.%s' % (request.environ["pylons.routes_dict"]["action"], name), delta=delta)

    def action_event_count(self, event_name, state=None, delta=1, true_name="success", false_name="fail"):
        counter_name = 'event.%s' % event_name
        if state == True:
            self.action_count(counter_name, true_name, delta=delta)
        elif state == False:
            self.action_count(counter_name, false_name, delta=delta)
        self.action_count(counter_name, 'total', delta=delta)

    def cache_count(self, name, delta=1):
        counter = self.get_counter('cache')
        if counter and random.random() < self.CACHE_SAMPLE_RATE:
            counter.increment(name, delta=delta)

    def amqp_processor(self, queue_name):
        """Decorator for recording stats for amqp queue consumers/handlers."""
        def decorator(processor):
            def wrap_processor(msgs, *args):
                # Work the same for amqp.consume_items and amqp.handle_items.
                msg_tup = utils.tup(msgs)

                start = time.time()
                try:
                    return processor(msgs, *args)
                finally:
                    service_time = (time.time() - start) / len(msg_tup)
                    for msg in msg_tup:
                        self.transact('amqp.%s' % queue_name, service_time)
            return wrap_processor
        return decorator

    def cassandra_event(self, operation, column_families, success,
                        service_time):
        if not isinstance(column_families, list):
            column_families = [column_families]
        for cf in column_families:
            key = '.'.join([
                cf, operation, self.CASSANDRA_KEY_SUFFIXES[success]])
            self.cassandra_events[key + '.time'] += service_time
            self.cassandra_events[key] += 1

    def flush_cassandra_events(self):
        events = self.cassandra_events
        self.cassandra_events = collections.defaultdict(int)
        if self.connection:
            data = {}
            for k, v in events.iteritems():
                if k.endswith('.time'):
                    suffix = '|ms'
                    # these stats get stored under timers, so chop off ".time"
                    k = k[:-5]
                    if k.endswith('.ok'):
                        # only report the mean over the duration of this request
                        v /= events.get(k, 1)
                        # chop off the ".ok" since we aren't storing error times
                        k = k[:-3]
                else:
                    suffix = '|c'
                data['cassandra.' + k] = str(v) + suffix
            self.connection.send(data)

class CacheStats:
    def __init__(self, parent, cache_name):
        self.parent = parent
        self.cache_name = cache_name
        self.hit_stat_name = '%s.hit' % self.cache_name
        self.miss_stat_name = '%s.miss' % self.cache_name
        self.total_stat_name = '%s.total' % self.cache_name

    def cache_hit(self, delta=1):
        if delta:
            self.parent.cache_count(self.hit_stat_name, delta=delta)
            self.parent.cache_count(self.total_stat_name, delta=delta)

    def cache_miss(self, delta=1):
        if delta:
            self.parent.cache_count(self.miss_stat_name, delta=delta)
            self.parent.cache_count(self.total_stat_name, delta=delta)

class StatsCollectingConnectionPool(pool.ConnectionPool):
    def __init__(self, keyspace, stats=None, *args, **kwargs):
        pool.ConnectionPool.__init__(self, keyspace, *args, **kwargs)
        self.stats = stats

    def _get_new_wrapper(self, server):
        cf_types = (columnfamily.ColumnParent, columnfamily.ColumnPath)

        def get_cf_name_from_args(args, kwargs):
            for v in args:
                if isinstance(v, cf_types):
                    return v.column_family
            for v in kwargs.itervalues():
                if isinstance(v, cf_types):
                    return v.column_family
            return None

        def get_cf_name_from_batch_mutation(args, kwargs):
            cf_names = set()
            mutation_map = args[0]
            for key_mutations in mutation_map.itervalues():
                cf_names.update(key_mutations)
            return list(cf_names)

        instrumented_methods = dict(
            get=get_cf_name_from_args,
            get_slice=get_cf_name_from_args,
            multiget_slice=get_cf_name_from_args,
            get_count=get_cf_name_from_args,
            multiget_count=get_cf_name_from_args,
            get_range_slices=get_cf_name_from_args,
            get_indexed_slices=get_cf_name_from_args,
            insert=get_cf_name_from_args,
            batch_mutate=get_cf_name_from_batch_mutation,
            add=get_cf_name_from_args,
            remove=get_cf_name_from_args,
            remove_counter=get_cf_name_from_args,
            truncate=lambda args, kwargs: args[0],
        )

        def record_error(method_name, cf_name, service_time):
            if cf_name and self.stats:
                self.stats.cassandra_event(method_name, cf_name, False,
                                           service_time)

        def record_success(method_name, cf_name, service_time):
            if cf_name and self.stats:
                self.stats.cassandra_event(method_name, cf_name, True,
                                           service_time)

        def instrument(f, get_cf_name):
            def call_with_instrumentation(*args, **kwargs):
                cf_name = get_cf_name(args, kwargs)
                start = time.time()
                try:
                    result = f(*args, **kwargs)
                except:
                    record_error(f.__name__, cf_name, time.time() - start)
                    raise
                else:
                    record_success(f.__name__, cf_name, time.time() - start)
                    return result
            return call_with_instrumentation

        wrapper = pool.ConnectionPool._get_new_wrapper(self, server)
        for method_name, get_cf_name in instrumented_methods.iteritems():
            f = getattr(wrapper, method_name)
            setattr(wrapper, method_name, instrument(f, get_cf_name))
        return wrapper

