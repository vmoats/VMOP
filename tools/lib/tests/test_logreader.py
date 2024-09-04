import capnp
import contextlib
import io
import shutil
import tempfile
import os
import pytest
import requests
import http.server

from parameterized import parameterized
from openpilot.selfdrive.test.helpers import http_server_context

from cereal import log as capnp_log
from openpilot.tools.lib.logreader import LogIterable, LogReader, auto_source, auto_strategy, apply_strategy, comma_api_source, \
                                          parse_indirect, ReadMode, InternalUnavailableException
from openpilot.tools.lib.route import SegmentRange
from openpilot.tools.lib.comma_car_segments import get_repo_url
from openpilot.tools.lib.url_file import URLFileException

NUM_SEGS = 17  # number of segments in the test route
ALL_SEGS = list(range(NUM_SEGS))
TEST_ROUTE = "344c5c15b34f2d8a/2024-01-03--09-37-12"
QLOG_FILE = "https://commadataci.blob.core.windows.net/openpilotci/0375fdf7b1ce594d/2019-06-13--08-32-25/3/qlog.bz2"


def noop(segment: LogIterable):
  return segment


@contextlib.contextmanager
def setup_source_scenario(mocker, is_internal=False):
  internal_source_mock = mocker.patch("openpilot.tools.lib.logreader.internal_source")
  internal_source_mock.__name__ = internal_source_mock._mock_name

  openpilotci_source_mock = mocker.patch("openpilot.tools.lib.logreader.openpilotci_source")
  openpilotci_source_mock.__name__ = openpilotci_source_mock._mock_name

  comma_api_source_mock = mocker.patch("openpilot.tools.lib.logreader.comma_api_source")
  comma_api_source_mock.__name__ = comma_api_source_mock._mock_name

  if is_internal:
    internal_source_mock.return_value = [QLOG_FILE]
  else:
    internal_source_mock.side_effect = InternalUnavailableException

  openpilotci_source_mock.return_value = [None]
  comma_api_source_mock.return_value = [QLOG_FILE]

  yield


class LogReaderTestRequestHandler(http.server.BaseHTTPRequestHandler):
  FILE_EXISTS = True

  def do_GET(self):
    if self.FILE_EXISTS:
      self.send_response(206 if "Range" in self.headers else 200, b'1234')
    else:
      self.send_response(404)
    self.end_headers()

  def do_HEAD(self):
    if self.FILE_EXISTS:
      self.send_response(200)
      self.send_header("Content-Length", "4")
    else:
      self.send_response(404)
    self.end_headers()


@pytest.fixture
def host():
  with http_server_context(handler=LogReaderTestRequestHandler) as (host, port):
    yield f"http://{host}:{port}"


class TestLogReader:
  @parameterized.expand([
    (f"{TEST_ROUTE}", ALL_SEGS),
    (f"{TEST_ROUTE.replace('/', '|')}", ALL_SEGS),
    (f"{TEST_ROUTE}--0", [0]),
    (f"{TEST_ROUTE}--5", [5]),
    (f"{TEST_ROUTE}/0", [0]),
    (f"{TEST_ROUTE}/5", [5]),
    (f"{TEST_ROUTE}/0:10", ALL_SEGS[0:10]),
    (f"{TEST_ROUTE}/0:0", []),
    (f"{TEST_ROUTE}/4:6", ALL_SEGS[4:6]),
    (f"{TEST_ROUTE}/0:-1", ALL_SEGS[0:-1]),
    (f"{TEST_ROUTE}/:5", ALL_SEGS[:5]),
    (f"{TEST_ROUTE}/2:", ALL_SEGS[2:]),
    (f"{TEST_ROUTE}/2:-1", ALL_SEGS[2:-1]),
    (f"{TEST_ROUTE}/-1", [ALL_SEGS[-1]]),
    (f"{TEST_ROUTE}/-2", [ALL_SEGS[-2]]),
    (f"{TEST_ROUTE}/-2:-1", ALL_SEGS[-2:-1]),
    (f"{TEST_ROUTE}/-4:-2", ALL_SEGS[-4:-2]),
    (f"{TEST_ROUTE}/:10:2", ALL_SEGS[:10:2]),
    (f"{TEST_ROUTE}/5::2", ALL_SEGS[5::2]),
    (f"https://useradmin.comma.ai/?onebox={TEST_ROUTE}", ALL_SEGS),
    (f"https://useradmin.comma.ai/?onebox={TEST_ROUTE.replace('/', '|')}", ALL_SEGS),
    (f"https://useradmin.comma.ai/?onebox={TEST_ROUTE.replace('/', '%7C')}", ALL_SEGS),
  ])
  def test_indirect_parsing(self, identifier, expected):
    parsed = parse_indirect(identifier)
    sr = SegmentRange(parsed)
    assert list(sr.seg_idxs) == expected, identifier

  @parameterized.expand([
    (f"{TEST_ROUTE}", f"{TEST_ROUTE}"),
    (f"{TEST_ROUTE.replace('/', '|')}", f"{TEST_ROUTE}"),
    (f"{TEST_ROUTE}--5", f"{TEST_ROUTE}/5"),
    (f"{TEST_ROUTE}/0/q", f"{TEST_ROUTE}/0/q"),
    (f"{TEST_ROUTE}/5:6/r", f"{TEST_ROUTE}/5:6/r"),
    (f"{TEST_ROUTE}/5", f"{TEST_ROUTE}/5"),
  ])
  def test_canonical_name(self, identifier, expected):
    sr = SegmentRange(identifier)
    assert str(sr) == expected

  @pytest.mark.parametrize("cache_enabled", [True, False])
  def test_direct_parsing(self, mocker, cache_enabled):
    file_exists_mock = mocker.patch("openpilot.tools.lib.logreader.file_exists")
    os.environ["FILEREADER_CACHE"] = "1" if cache_enabled else "0"
    qlog = tempfile.NamedTemporaryFile(mode='wb', delete=False)

    with requests.get(QLOG_FILE, stream=True) as r:
      with qlog as f:
        shutil.copyfileobj(r.raw, f)

    for f in [QLOG_FILE, qlog.name]:
      l = len(list(LogReader(f)))
      assert l > 100

    with pytest.raises(URLFileException) if not cache_enabled else pytest.raises(AssertionError):
      l = len(list(LogReader(QLOG_FILE.replace("/3/", "/200/"))))

    # file_exists should not be called for direct files
    assert file_exists_mock.call_count == 0

  @parameterized.expand([
    (f"{TEST_ROUTE}///",),
    (f"{TEST_ROUTE}---",),
    (f"{TEST_ROUTE}/-4:--2",),
    (f"{TEST_ROUTE}/-a",),
    (f"{TEST_ROUTE}/j",),
    (f"{TEST_ROUTE}/0:1:2:3",),
    (f"{TEST_ROUTE}/:::3",),
    (f"{TEST_ROUTE}3",),
    (f"{TEST_ROUTE}-3",),
    (f"{TEST_ROUTE}--3a",),
  ])
  def test_bad_ranges(self, segment_range):
    with pytest.raises(AssertionError):
      _ = SegmentRange(segment_range).seg_idxs

  @pytest.mark.parametrize("segment_range, api_call", [
    (f"{TEST_ROUTE}/0", False),
    (f"{TEST_ROUTE}/:2", False),
    (f"{TEST_ROUTE}/0:", True),
    (f"{TEST_ROUTE}/-1", True),
    (f"{TEST_ROUTE}", True),
  ])
  def test_slicing_api_call(self, mocker, segment_range, api_call):
    max_seg_mock = mocker.patch("openpilot.tools.lib.route.get_max_seg_number_cached")
    max_seg_mock.return_value = NUM_SEGS
    _ = SegmentRange(segment_range).seg_idxs
    assert api_call == max_seg_mock.called

  @pytest.mark.slow
  def test_modes(self):
    qlog_len = len(list(LogReader(f"{TEST_ROUTE}/0", ReadMode.QLOG)))
    rlog_len = len(list(LogReader(f"{TEST_ROUTE}/0", ReadMode.RLOG)))

    assert qlog_len * 6 < rlog_len

  @pytest.mark.slow
  def test_modes_from_name(self):
    qlog_len = len(list(LogReader(f"{TEST_ROUTE}/0/q")))
    rlog_len = len(list(LogReader(f"{TEST_ROUTE}/0/r")))

    assert qlog_len * 6 < rlog_len

  @pytest.mark.slow
  def test_list(self):
    qlog_len = len(list(LogReader(f"{TEST_ROUTE}/0/q")))
    qlog_len_2 = len(list(LogReader([f"{TEST_ROUTE}/0/q", f"{TEST_ROUTE}/0/q"])))

    assert qlog_len * 2 == qlog_len_2

  @pytest.mark.slow
  def test_multiple_iterations(self, mocker):
    init_mock = mocker.patch("openpilot.tools.lib.logreader._LogFileReader")
    lr = LogReader(f"{TEST_ROUTE}/0/q")
    qlog_len1 = len(list(lr))
    qlog_len2 = len(list(lr))

    # ensure we don't create multiple instances of _LogFileReader, which means downloading the files twice
    assert init_mock.call_count == 1

    assert qlog_len1 == qlog_len2

  @pytest.mark.slow
  def test_helpers(self):
    lr = LogReader(f"{TEST_ROUTE}/0/q")
    assert lr.first("carParams").carFingerprint == "SUBARU OUTBACK 6TH GEN"
    assert 0 < len(list(lr.filter("carParams"))) < len(list(lr))

  @parameterized.expand([(True,), (False,)])
  @pytest.mark.slow
  def test_run_across_segments(self, cache_enabled):
    os.environ["FILEREADER_CACHE"] = "1" if cache_enabled else "0"
    lr = LogReader(f"{TEST_ROUTE}/0:4")
    assert len(lr.run_across_segments(4, noop)) == len(list(lr))

  @pytest.mark.slow
  def test_auto_mode(self, subtests, mocker):
    lr = LogReader(f"{TEST_ROUTE}/0/q")
    qlog_len = len(list(lr))
    log_paths_mock = mocker.patch("openpilot.tools.lib.route.Route.log_paths")
    log_paths_mock.return_value = [None] * NUM_SEGS
    # Should fall back to qlogs since rlogs are not available

    with subtests.test("interactive_yes"):
      mocker.patch("sys.stdin", new=io.StringIO("y\n"))
      lr = LogReader(f"{TEST_ROUTE}/0", default_mode=ReadMode.AUTO_INTERACTIVE, source=comma_api_source)
      log_len = len(list(lr))
      assert qlog_len == log_len

    with subtests.test("interactive_no"):
      mocker.patch("sys.stdin", new=io.StringIO("n\n"))
      with pytest.raises(AssertionError):
        lr = LogReader(f"{TEST_ROUTE}/0", default_mode=ReadMode.AUTO_INTERACTIVE, source=comma_api_source)

    with subtests.test("non_interactive"):
      lr = LogReader(f"{TEST_ROUTE}/0", default_mode=ReadMode.AUTO, source=comma_api_source)
      log_len = len(list(lr))
      assert qlog_len == log_len

  @pytest.mark.parametrize("is_internal", [True, False])
  @pytest.mark.slow
  def test_auto_source_scenarios(self, mocker, is_internal):
    lr = LogReader(QLOG_FILE)
    qlog_len = len(list(lr))

    with setup_source_scenario(mocker, is_internal=is_internal):
      lr = LogReader(f"{TEST_ROUTE}/0/q")
      log_len = len(list(lr))
      assert qlog_len == log_len

  @pytest.mark.slow
  def test_sort_by_time(self):
    msgs = list(LogReader(f"{TEST_ROUTE}/0/q"))
    assert msgs != sorted(msgs, key=lambda m: m.logMonoTime)

    msgs = list(LogReader(f"{TEST_ROUTE}/0/q", sort_by_time=True))
    assert msgs == sorted(msgs, key=lambda m: m.logMonoTime)

  def test_only_union_types(self):
    with tempfile.NamedTemporaryFile() as qlog:
      # write valid Event messages
      num_msgs = 100
      with open(qlog.name, "wb") as f:
        f.write(b"".join(capnp_log.Event.new_message().to_bytes() for _ in range(num_msgs)))

      msgs = list(LogReader(qlog.name))
      assert len(msgs) == num_msgs
      [m.which() for m in msgs]

      # append non-union Event message
      event_msg = capnp_log.Event.new_message()
      non_union_bytes = bytearray(event_msg.to_bytes())
      non_union_bytes[event_msg.total_size.word_count * 8] = 0xff  # set discriminant value out of range using Event word offset
      with open(qlog.name, "ab") as f:
        f.write(non_union_bytes)

      # ensure new message is added, but is not a union type
      msgs = list(LogReader(qlog.name))
      assert len(msgs) == num_msgs + 1
      with pytest.raises(capnp.KjException):
        [m.which() for m in msgs]

      # should not be added when only_union_types=True
      msgs = list(LogReader(qlog.name, only_union_types=True))
      assert len(msgs) == num_msgs
      [m.which() for m in msgs]

  def test_source_rlogs_not_available_qlogs_available(self, mocker, host):
    mock_openpilotci_source = mocker.patch("openpilot.tools.lib.logreader.openpilotci_source")
    mock_openpilotci_source.__name__ = mock_openpilotci_source._mock_name
    mock_comma_api_source = mocker.patch("openpilot.tools.lib.logreader.comma_api_source")
    mock_comma_api_source.__name__ = mock_comma_api_source._mock_name
    mock_comma_car_segments_source = mocker.patch("openpilot.tools.lib.logreader.comma_car_segments_source")
    mock_comma_car_segments_source.__name__ = mock_comma_car_segments_source._mock_name
    mock_auto_strategy = mocker.patch("openpilot.tools.lib.logreader.auto_strategy")

    mock_comma_car_segments_source.side_effect = [Exception("Rlogs not available")]

    def default_valid(fn):
      return fn is not None

    for mode in [ReadMode.RLOG, ReadMode.QLOG, ReadMode.AUTO]:
      mock_openpilotci_source.side_effect = lambda sr, current_mode:(
        apply_strategy(current_mode, [None], [f'{host}/openpilotci/qlog']))

      mock_comma_api_source.side_effect = lambda sr, current_mode:(
        apply_strategy(current_mode, [None], [f'{host}/comma_api/qlog'], valid_file=default_valid))

      if mode == ReadMode.AUTO:
        mock_auto_strategy.side_effect = lambda rlog, qlog, interactive, valid_file=None:(
          auto_strategy([None], qlog, interactive, valid_file))

      if mode == ReadMode.RLOG:
        with pytest.raises(Exception): # noqa
          auto_source(SegmentRange(TEST_ROUTE), mode)
      else:
        assert auto_source(SegmentRange(TEST_ROUTE), mode) == [f'{host}/openpilotci/qlog']

  def test_source_rlogs_not_available_commaapi(self, mocker, host):
    mock_openpilotci_source = mocker.patch("openpilot.tools.lib.logreader.openpilotci_source")
    mock_openpilotci_source.__name__ = mock_openpilotci_source._mock_name
    mock_comma_api_source = mocker.patch("openpilot.tools.lib.logreader.comma_api_source")
    mock_comma_api_source.__name__ = mock_comma_api_source._mock_name
    mock_comma_car_segments_source = mocker.patch("openpilot.tools.lib.logreader.comma_car_segments_source")
    mock_comma_car_segments_source.__name__ = mock_comma_car_segments_source._mock_name
    mock_parse_lfs_pointer= mocker.patch("openpilot.tools.lib.comma_car_segments.parse_lfs_pointer")
    mock_parse_lfs_pointer.return_value = ("abcd1234ijkl1234abcd", "1234")
    mock_get_length_online = mocker.patch("openpilot.tools.lib.url_file.URLFile.get_length_online")
    mock_get_length_online.return_value = -1

    class MockRequestHead:
      def __init__(self):
        self.status_code = 404
        self.headers = {'content-type': 'text/plain; charset=UTF-8'}

    class MockRequestGet:
      def __init__(self):
        self.status_code = 200
        self.text = '''version https://git-lfs.github.com/spec/v1
oid sha256:abcd1234ijkl1234abcd
size 1234'''

    class MockRequestPost:
      def __init__(self):
        self.ok = True
        self.json_data = {
        "objects": [
          {
            "oid": "abcd1234ijkl1234abcd",
            "size": 1234,
            "actions": {
              "download": {
                "href": f"{host}abcd1234ijkl1234abcd",
                "expires_at": "2023-01-01T00:00:00Z"
              }
            }
          }
        ]
      }

      def json(self):
        return self.json_data

    def default_valid(fn):
      return fn is not None

    mock_request_head = mocker.patch("requests.head")
    mock_request_head.return_value = MockRequestHead()
    mock_request_get = mocker.patch("requests.get")
    mock_request_get.return_value = MockRequestGet()
    mock_request_post = mocker.patch("requests.post")
    mock_request_post.return_value = MockRequestPost()

    mock_comma_car_segments_source.side_effect = [get_repo_url(f'{host}/rlog')]

    mock_openpilotci_source.side_effect = lambda sr, current_mode:(
      apply_strategy(current_mode, [f'{host}/openpilotci/rlog'], [f'{host}/openpilotci/qlog']))

    mock_comma_api_source.side_effect = lambda sr, current_mode:(
      apply_strategy(current_mode, [None], [f'{host}/comma_api/qlog'], valid_file=default_valid))

    with pytest.raises(Exception): # noqa
      auto_source(SegmentRange(TEST_ROUTE), ReadMode.RLOG)

    assert mock_parse_lfs_pointer.called, "parse_lfs_pointer function was not called"


  def test_source_rlogs_segments_qlogs_rest(self, mocker, host):
    mock_openpilotci_source = mocker.patch("openpilot.tools.lib.logreader.openpilotci_source")
    mock_openpilotci_source.__name__ = mock_openpilotci_source._mock_name
    mock_comma_api_source = mocker.patch("openpilot.tools.lib.logreader.comma_api_source")
    mock_comma_api_source.__name__ = mock_comma_api_source._mock_name
    mock_comma_car_segments_source = mocker.patch("openpilot.tools.lib.logreader.comma_car_segments_source")
    mock_comma_car_segments_source.__name__ = mock_comma_car_segments_source._mock_name
    mock_auto_strategy = mocker.patch("openpilot.tools.lib.logreader.auto_strategy")

    rlog_paths = [f'{host}/0/rlog', None, f'{host}/2/rlog']
    qlog_paths = [f'{host}/{seg}/qlog' for seg in range(3)]
    mock_comma_car_segments_source.side_effect = [Exception("Could not access to rlog")]

    def default_valid(fn):
      return fn is not None

    for mode in [ReadMode.RLOG, ReadMode.AUTO]:
      mock_openpilotci_source.side_effect = lambda sr, current_mode:(
        apply_strategy(current_mode, rlog_paths, qlog_paths))

      mock_comma_api_source.side_effect = lambda sr, current_mode:(
        apply_strategy(current_mode, rlog_paths, qlog_paths, valid_file=default_valid))

      if mode == ReadMode.AUTO:
        mock_auto_strategy.side_effect = lambda rlog, qlog, interactive, valid_file=None:(
          auto_strategy(rlog_paths, qlog_paths, interactive, default_valid))

      if mode == ReadMode.RLOG:
        with pytest.raises(Exception): # noqa
          auto_source(SegmentRange(TEST_ROUTE), mode)
      else:
        assert auto_source(SegmentRange(TEST_ROUTE), mode) == [rlog_paths[i] if rlog_paths[i] is not None else \
                                                               qlog_paths[i] for i in range(len(rlog_paths))]

  def test_source_no_logs_available(self, mocker):
    mock_openpilotci_source = mocker.patch("openpilot.tools.lib.logreader.openpilotci_source")
    mock_openpilotci_source.__name__ = mock_openpilotci_source._mock_name
    mock_comma_api_source = mocker.patch("openpilot.tools.lib.logreader.comma_api_source")
    mock_comma_api_source.__name__ = mock_comma_api_source._mock_name
    mock_comma_car_segments_source = mocker.patch("openpilot.tools.lib.logreader.comma_car_segments_source")
    mock_comma_car_segments_source.__name__ = mock_comma_car_segments_source._mock_name
    mock_auto_strategy = mocker.patch("openpilot.tools.lib.logreader.auto_strategy")

    mock_comma_car_segments_source.side_effect = Exception("Rlogs not available")

    def default_valid(fn):
      return fn is not None

    for mode in list(ReadMode):
      if mode != ReadMode.SANITIZED:
        mock_openpilotci_source.side_effect = lambda sr, current_mode:(
          apply_strategy(current_mode, [None], [None]))

        mock_comma_api_source.side_effect = lambda sr, current_mode:(
          apply_strategy(current_mode, [None], [None], valid_file=default_valid))

      if mode == ReadMode.AUTO or mode == ReadMode.AUTO_INTERACTIVE:
        mock_auto_strategy.side_effect = lambda rlog, qlog, interactive, valid_file=None:(
          auto_strategy([None], [None], interactive, valid_file))

      with pytest.raises(Exception): # noqa
        auto_source(SegmentRange(TEST_ROUTE), mode)
