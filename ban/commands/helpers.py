import csv
from datetime import timedelta
import getpass
import os
import pkgutil
import sys
from multiprocessing.pool import RUN, IMapUnorderedIterator, Pool
from importlib import import_module
from pathlib import Path

import decorator
from progressist import ProgressBar

from ban.auth.models import Session, Client, User
from ban.db.model import SelectQuery
from ban.core import context, config
from ban.core.versioning import Diff


def load_commands():
    from ban import commands
    prefix = commands.__name__ + "."
    for importer, modname, ispkg in pkgutil.iter_modules(commands.__path__,
                                                         prefix):
        if ispkg or modname == __name__:
            continue
        import_module(modname, package=commands.__name__)


def load_csv(path_or_file, encoding='utf-8'):
    if isinstance(path_or_file, (str, Path)):
        path = Path(path_or_file)
        if not path.exists():
            abort('Path does not exist: {}'.format(path))
        path_or_file = path.open(encoding=encoding)
    extract = path_or_file.read(4096)
    try:
        dialect = csv.Sniffer().sniff(extract)
    except csv.Error:
        dialect = csv.unix_dialect()
    path_or_file.seek(0)
    content = path_or_file.read()
    path_or_file.close()
    return csv.DictReader(content.splitlines(), dialect=dialect)


def iter_file(path, formatter=lambda x: x):
    path = Path(path)
    if not path.exists():
        abort('Path does not exist: {}'.format(path))
    with path.open() as f:
        for l in f:
            yield formatter(l)


def abort(msg):
    sys.stderr.write("\n" + msg)
    sys.exit(1)


class Bar(ProgressBar):
    template = ('Progress: |{animation}| {percent} ({done}/{total}) '
                '| ETA: {eta} | {elapsed}')


def collect_report(func, chunk):
    # This is a process reporter instance.
    reporter = context.get('reporter')
    results = func(*chunk)
    reports = reporter._reports.copy()
    reporter.clear()
    return results, reports


class ChunkedPool(Pool):

    @classmethod
    def _get_tasks_from_query(cls, func, query, chunksize):
        for idx in range(0, query.count(), chunksize):
            yield (func, list(query.limit(chunksize).offset(idx)))

    def imap_unordered(self, func, iterable, chunksize):
        """Customized version of imap_unordered.
        Directly send chunks to func, instead of iterating in each process and
        sending one by one.
        Original:
        https://hg.python.org/cpython/file/tip/Lib/multiprocessing/pool.py#l271
        Other tried options:
        - map_async: makes a list(iterable), so it loads all the data for each
          process into RAM
        - apply_async: needs manual chunking
        """
        assert self._state == RUN
        if isinstance(iterable, SelectQuery):
            task_batches = self._get_tasks_from_query(func, iterable,
                                                      chunksize)
        else:
            task_batches = self._get_tasks(func, iterable, chunksize)
        result = IMapUnorderedIterator(self._cache)
        tasks = ((result._job, i, collect_report, (func, chunk), {})
                 for i, (_, chunk) in enumerate(task_batches))
        self._taskqueue.put((tasks, result._set_length))
        return result


def batch(func, iterable, chunksize=1000, total=None, progress=True):
    # This is the main reporter instance.
    reporter = context.get('reporter')
    bar = Bar(total=total, throttle=timedelta(seconds=1))
    workers = int(config.get('WORKERS', os.cpu_count()))

    with ChunkedPool(processes=workers) as pool:
        try:
            for results, reports in pool.imap_unordered(func, iterable, chunksize):
                reporter.merge(reports)
                bar(step=len(results))
                yield from results
            bar.finish()
        except Exception as e:
            print("\n"+e.args[0])
            pool.terminate()


def prompt(text, default=..., confirmation=False, coerce=None, hidden=False):
    """Prompts a user for input.  This is a convenience function that can
    be used to prompt a user for input later.

    :param text: the text to show for the prompt.
    :param default: the default value to use if no input happens.  If this
                    is not given it will prompt until it's aborted.
    :param confirmation: asks for confirmation for the value.
    :param coerce: a callable to use to coerce the value.
    :param hidden: define if the input should be hidden (for password for eg.)
    """
    result = None
    func = getpass.getpass if hidden else input

    while 1:
        while 1:
            try:
                result = func('{}: '.format(text))
            except (KeyboardInterrupt, EOFError):
                abort('Aborted.')
            if result:
                break
            elif default is not ...:
                return default
        if coerce:
            try:
                result = coerce(result)
            except ValueError:
                sys.stderr.write('Wrong value for type {}'.format(type))
                continue
        if not confirmation:
            return result
        while 1:
            try:
                confirm = func('{} (again): '.format(text))
            except (KeyboardInterrupt, EOFError):
                abort('Aborted.')
            if confirm:
                if result == confirm:
                    return result
                print('Error: the two entered values do not match')


def confirm(text, default=None):
    """Ask for confirmation."""
    value = None
    if default:
        default_text = 'Yn'
    elif default is False:
        default_text = 'yN'
    else:
        default_text = 'yn'
    while 1:
        try:
            value = input('{} [{}]: '.format(text, default_text))
        except (KeyboardInterrupt, EOFError):
            abort('Aborted.')
        if value.lower() in ('y', 'yes'):
            return True
        if value.lower() in ('n', 'no'):
            return False
        if value == '' and default is not None:
            return default


@decorator.decorator
def session(func, *args, **kwargs):
    session = context.get('session')
    if not session:
        qs = User.select().where(User.is_staff == True)
        username = config.get('SESSION_USER')
        if username:
            qs = qs.where(User.username == username)
        try:
            user = qs.get()
        except User.DoesNotExist:
            abort('Admin user not found {}'.format(username or ''))
        session = Session.create(user=user, contributor_type='admin')
        context.set('session', session)
    return func(*args, **kwargs)


@decorator.decorator
def session_client(func, *args, **kwargs):
    clientname = context.get('clientname')
    contributor_type = context.get('contributor_type')
    try:
        client = Client.select().where(Client.name == clientname).get()
    except Client.DoesNotExist:
        raise Exception('Client not found {}'.format(clientname or ''))
    session = Session.create(client=client, contributor_type=contributor_type)
    context.set('session', session)
    return func(*args, **kwargs)


@decorator.decorator
def nodiff(func, *args, **kwargs):
    Diff.ACTIVE = False
    res = func(*args, **kwargs)
    Diff.ACTIVE = True
    return res


def file_len(f):
    l = sum(1 for line in f)
    f.seek(0)
    return l
