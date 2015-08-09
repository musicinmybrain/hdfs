#!/usr/bin/env python
# encoding: utf-8

"""Avro extension.

TODO

Without this extension:

.. code-block:: python

  with client.write(hdfs_path) as bytes_writer:
    fastavro.writer(bytes_writer, schema, records)

"""

from ...util import HdfsError
from json import dumps
import fastavro
import io
import logging as lg
import os
import posixpath as psp
import sys


_logger = lg.getLogger(__name__)

# def _write_header(fo, schema, codec, sync_marker):
#   """Write header, stripping spaces."""
#   utob = fastavro._writer.utob
#   header = {
#     'magic': fastavro._writer.MAGIC,
#     'meta': {
#       'avro.codec': utob(codec),
#       'avro.schema': utob(dumps(schema, separators=(',', ':'))),
#     },
#     'sync': sync_marker,
#   }
#   fastavro._writer.write_data(fo, header, fastavro._writer.HEADER_SCHEMA)

def _get_type(obj, allow_null=False):
  """Infer Avro type corresponding to a python object.

  :param obj: Python primitive.
  :param allow_null: Allow null values.

  """
  if allow_null:
    raise NotImplementedError('TODO')
  if isinstance(obj, bool):
    schema_type = 'boolean'
  elif isinstance(obj, string_types):
    schema_type = 'string'
  elif isinstance(obj, int):
    schema_type = 'int'
  elif isinstance(obj, long):
    schema_type = 'long'
  elif isinstance(obj, float):
    schema_type = 'float'
  return schema_type # TODO: Add array and record support.

def infer_schema(obj):
  """Infer schema from dictionary.

  :param obj: Dictionary.

  """
  return {
    'type': 'record',
    'name': 'element',
    'fields': [
      {'name': k, 'type': _get_type(v)}
      for k, v in obj.items()
    ]
  }


class _SeekableReader(object):

  """Customized reader for Avro.

  :param reader: Non-seekable reader.
  :param size: For testing.

  It detects reads of sync markers' sizes and will buffer these. Note that this
  reader is heavily particularized to how the `fastavro` library performs Avro
  decoding.

  """

  sync_size = 16

  def __init__(self, reader, size=None):
    self._reader = reader
    self._size = size or self.sync_size
    self._buffer = None
    self._saught = False

  def read(self, nbytes):
    buf = self._buffer
    if self._saught:
      assert buf
      missing_bytes = nbytes - len(buf)
      if missing_bytes < 0:
        chunk = buf[:nbytes]
        self._buffer = buf[nbytes:]
      else:
        chunk = buf
        if missing_bytes:
          chunk += self._reader.read(missing_bytes)
        self._buffer = None
        self._saught = False
    else:
      self._buffer = None
      chunk = self._reader.read(nbytes)
      if nbytes == self._size:
        self._buffer = chunk
    return chunk

  def seek(self, offset, whence):
    assert offset == - self._size
    assert whence == os.SEEK_CUR
    assert self._buffer
    self._saught = True


class AvroReader(object):

  """Lazy remote Avro file reader.

  :param client: :class:`hdfs.client.Client` instance.
  :param hdfs_path: Remote path.
  :param parts: Cf. :meth:`hdfs.client.Client.parts`.

  Usage:

  .. code-block:: python

    with AvroReader(client, 'foo.avro') as reader:
      schema = reader.schema # The remote file's Avro schema.
      content = reader.content # Content metadata (e.g. size).
      for record in reader:
        pass # and its records

  """

  def __init__(self, client, hdfs_path, parts=None):
    self.content = client.content(hdfs_path)
    self._schema = None
    if self.content['directoryCount']:
      # This is a folder.
      self._paths = [
        psp.join(hdfs_path, fname)
        for fname in client.parts(hdfs_path, parts)
      ]
    else:
      # This is a single file.
      self._paths = [hdfs_path]
    self._client = client

  def __enter__(self):

    def _reader():
      """Record generator over all part-files."""
      for path in self._paths:
        with self._client.read(path, chunk_size=0) as bytes_reader:
          avro_reader = fastavro.reader(_SeekableReader(bytes_reader))
          if not self._schema:
            yield avro_reader.schema
          for record in avro_reader:
            yield record

    self.records = _reader()
    self._schema = self.records.next() # Prime generator to get schema.
    return self

  def __exit__(self, exc_type, exc_value, traceback):
    self.records.close()

  def __iter__(self):
    return self.records

  @property
  def schema(self):
    """Get the underlying file's schema.

    The schema will only be available after entering the reader's corresponding
    `with` block.

    """
    if not self._schema:
      raise HdfsError('Schema not yet inferred.')
    return self._schema




class AvroWriter(object):

  """Write an Avro file on HDFS from python dictionaries.

  :param client: :class:`hdfs.client.Client` instance.
  :param hdfs_path: Remote path.
  :param records: Generator of records to write.
  :param schema: Avro schema. See :func:`infer_schema` for an easy way to
    generate schemas in most cases.
  :param \*\*kwargs: Keyword arguments forwarded to :meth:`Client.write`.

  Usage:

  .. code::

    with AvroWriter(client, 'data.avro') as writer:
      for record in records:
        writer.send(record)

  """

  def __init__(self, client, hdfs_path, schema=None, codec=None,
    sync_interval=None, sync_marker=None, **kwargs):
    self._hdfs_path = hdfs_path
    self._fo = client.write(hdfs_path, **kwargs)
    self._schema = schema
    self._codec = codec or 'null'
    self._sync_interval = sync_interval or 1000 * fastavro._writer.SYNC_SIZE
    self._sync_marker = sync_marker or os.urandom(fastavro._writer.SYNC_SIZE)
    self._writer = None
    _logger.info('Instantiated %r.', self)

  def __repr__(self):
    return '<AvroWriter(hdfs_path=%r)>' % (self._hdfs_path, )

  def __enter__(self):
    self._writer = self._write(self._fo.__enter__())
    try:
      self._writer.send(None) # Prime coroutine.
    except Exception: # pylint: disable=broad-except
      if not self._fo.__exit__(*sys.exc_info()):
        raise
    else:
      return self

  def __exit__(self, *exc_info):
    self._writer.close()
    return self._fo.__exit__(*exc_info)

  @property
  def schema(self):
    """Avro schema."""
    if not self._schema:
      raise HdfsError('Schema not yet inferred.')
    return self._schema

  def write(self, record):
    """Store a record.

    :param record: Record object to store.

    """
    self._writer.send(record)

  def _write(self, fo):
    """Coroutine to write to a file object."""
    buf = fastavro._writer.MemoryIO()
    block_writer = fastavro._writer.BLOCK_WRITERS[self._codec]
    n_records = 0
    n_block_records = 0

    # Cache a few variables.
    sync_interval = self._sync_interval
    write_data = fastavro._writer.write_data
    write_long = fastavro._writer.write_long

    def dump_header():
      """Write header."""
      fastavro._writer.write_header(
        fo,
        self._schema,
        self._codec,
        self._sync_marker
      )
      _logger.debug('Wrote header. Sync marker: %r', self._sync_marker)
      fastavro._writer.acquaint_schema(self._schema)

    def dump_data():
      """Write contents of memory buffer to file object."""
      write_long(fo, n_block_records)
      block_writer(fo, buf.getvalue())
      fo.write(self._sync_marker)
      buf.truncate(0)
      _logger.debug('Dumped block of %s records.', n_block_records)

    if self._schema:
      dump_header()
    try:
      while True:
        record = (yield)
        if not n_records:
          if not self._schema:
            self._schema = None # TODO: Infer schema.
            _logger.info('Inferred schema: %s', dumps(self._schema))
            dump_header()
          schema = self._schema
        write_data(buf, record, schema)
        n_block_records += 1
        n_records += 1
        if buf.tell() >= sync_interval:
          dump_data()
          n_block_records = 0
    except GeneratorExit: # No more records.
      if buf.tell():
        dump_data()
      fo.flush()
      _logger.info('Finished writing %s records.', n_records)