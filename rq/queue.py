import times
from functools import total_ordering
from .proxy import conn
from .job import Job
from .exceptions import NoSuchJobError, UnpickleError


def compact(lst):
    return [item for item in lst if item is not None]


@total_ordering
class Queue(object):
    redis_queue_namespace_prefix = 'rq:queue:'

    @classmethod
    def all(cls):
        """Returns an iterable of all Queues.
        """
        prefix = cls.redis_queue_namespace_prefix
        return map(cls.from_queue_key, conn.keys('%s*' % prefix))

    @classmethod
    def from_queue_key(cls, queue_key):
        """Returns a Queue instance, based on the naming conventions for naming
        the internal Redis keys.  Can be used to reverse-lookup Queues by their
        Redis keys.
        """
        prefix = cls.redis_queue_namespace_prefix
        if not queue_key.startswith(prefix):
            raise ValueError('Not a valid RQ queue key: %s' % (queue_key,))
        name = queue_key[len(prefix):]
        return Queue(name)

    def __init__(self, name='default'):
        prefix = self.redis_queue_namespace_prefix
        self.name = name
        self._key = '%s%s' % (prefix, name)

    @property
    def key(self):
        """Returns the Redis key for this Queue."""
        return self._key

    def is_empty(self):
        """Returns whether the current queue is empty."""
        return self.count == 0

    @property
    def job_ids(self):
        """Returns a list of all job IDS in the queue."""
        return conn.lrange(self.key, 0, -1)

    @property
    def jobs(self):
        """Returns a list of all (valid) jobs in the queue."""
        def safe_fetch(job_id):
            try:
                job = Job.fetch(job_id)
            except UnpickleError:
                return None
            return job

        return compact([safe_fetch(job_id) for job_id in self.job_ids])

    @property
    def count(self):
        """Returns a count of all messages in the queue."""
        return conn.llen(self.key)


    def push_job_id(self, job_id):
        """Pushes a job ID on the corresponding Redis queue."""
        conn.rpush(self.key, job_id)

    def enqueue(self, f, *args, **kwargs):
        """Creates a job to represent the delayed function call and enqueues it.

        Expects the function to call, along with the arguments and keyword
        arguments.
        """
        if f.__module__ == '__main__':
            raise ValueError('Functions from the __main__ module cannot be processed by workers.')

        job = Job.for_call(f, *args, **kwargs)
        return self.enqueue_job(job)

    def enqueue_job(self, job):
        """Enqueues a job for delayed execution."""
        job.origin = self.name
        job.enqueued_at = times.now()
        job.save()
        self.push_job_id(job.id)
        return job

    def requeue(self, job):
        """Requeues an existing (typically a failed job) onto the queue."""
        raise NotImplementedError('Implement this')

    def pop_job_id(self):
        """Pops a given job ID from this Redis queue."""
        return conn.lpop(self.key)

    @classmethod
    def lpop(cls, queue_keys, blocking):
        """Helper method.  Intermediate method to abstract away from some Redis
        API details, where LPOP accepts only a single key, whereas BLPOP accepts
        multiple.  So if we want the non-blocking LPOP, we need to iterate over
        all queues, do individual LPOPs, and return the result.

        Until Redis receives a specific method for this, we'll have to wrap it
        this way.
        """
        if blocking:
            queue_key, job_id = conn.blpop(queue_keys)
            return queue_key, job_id
        else:
            for queue_key in queue_keys:
                blob = conn.lpop(queue_key)
                if blob is not None:
                    return queue_key, blob
            return None

    def dequeue(self):
        """Dequeues the front-most job from this queue.

        Returns a Job instance, which can be executed or inspected.
        """
        job_id = self.pop_job_id()
        if job_id is None:
            return None
        try:
            job = Job.fetch(job_id)
        except NoSuchJobError as e:
            # Silently pass on jobs that don't exist (anymore),
            # and continue by reinvoking itself recursively
            return self.dequeue()
        except UnpickleError as e:
            # Attach queue information on the exception for improved error
            # reporting
            e.queue = self
            raise e
        return job

    @classmethod
    def dequeue_any(cls, queues, blocking):
        """Class method returning the Job instance at the front of the given set
        of Queues, where the order of the queues is important.

        When all of the Queues are empty, depending on the `blocking` argument,
        either blocks execution of this function until new messages arrive on
        any of the queues, or returns None.
        """
        queue_keys = [q.key for q in queues]
        result = cls.lpop(queue_keys, blocking)
        if result is None:
            return None
        queue_key, job_id = result
        queue = Queue.from_queue_key(queue_key)
        try:
            job = Job.fetch(job_id)
        except NoSuchJobError:
            # Silently pass on jobs that don't exist (anymore),
            # and continue by reinvoking the same function recursively
            return cls.dequeue_any(queues, blocking)
        except UnpickleError as e:
            # Attach queue information on the exception for improved error
            # reporting
            e.job_id = job_id
            e.queue = queue
            raise e
        return job, queue


    # Total ordering defition (the rest of the required Python methods are
    # auto-generated by the @total_ordering decorator)
    def __eq__(self, other):
        if not isinstance(other, Queue):
            raise TypeError('Cannot compare queues to other objects.')
        return self.name == other.name

    def __lt__(self, other):
        if not isinstance(other, Queue):
            raise TypeError('Cannot compare queues to other objects.')
        return self.name < other.name

    def __hash__(self):
        return hash(self.name)


    def __repr__(self):
        return 'Queue(%r)' % (self.name,)

    def __str__(self):
        return '<Queue \'%s\'>' % (self.name,)