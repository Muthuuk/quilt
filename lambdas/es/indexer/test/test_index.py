"""
Tests for the ES indexer. This function consumes events from SQS.
"""
from copy import deepcopy
from gzip import compress
from io import BytesIO
import json
import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import unquote_plus

import boto3
from botocore import UNSIGNED
from botocore.client import Config
from botocore.stub import Stubber
import pytest
import responses

from document_queue import RetryError
from .. import index


BASE_DIR = Path(__file__).parent / 'data'

CREATE_EVENT_TYPES = {
    "ObjectCreated:Put",
    "ObjectCreated:Copy",
    "ObjectCreated:Post",
    "ObjectCreated:CompleteMultipartUpload"
}
UNKNOWN_EVENT_TYPE = "Event:WeNeverHeardOf"
# See the following AWS docs for event structure:
EVENT_CORE = {
    "awsRegion": "us-east-1",
    "eventName": "ObjectCreated:Put",
    "eventSource": "aws:s3",
    "eventTime": "2020-05-22T00:32:20.515Z",
    "eventVersion": "2.1",
    "requestParameters": {"sourceIPAddress": "127.0.0.1"},
    "responseElements": {
        "x-amz-id-2": "EXAMPLE123/5678abcdefghijklambdaisawesome/mnopqrstuvwxyzABCDEFGH",
        "x-amz-request-id": "EXAMPLE123456789"
    },
    "s3": {
        "bucket": {
            "arn": "arn:aws:s3:::test-bucket",
            "name": "test-bucket",
            "ownerIdentity": {
                "principalId": "EXAMPLE"
            }
        },
        "configurationId": "testConfigRule",
        "object": {
            "key": "hello+world.txt",
            "sequencer": "0A1B2C3D4E5F678901"
        },
        "s3SchemaVersion": "1.0"
    },
    "userIdentity": {"principalId": "EXAMPLE"}
}


def _check_event(synthetic, organic):
    # Ensure that synthetic events have the same shape as actual organic ones,
    # and that overridden properties like bucket, key, eTag are properly set
    # same keys at top level
    assert organic.keys() == synthetic.keys()
    # same value types (values might differ and that's OK)
    assert {type(v) for v in organic.values()} == \
        {type(v) for v in synthetic.values()}
    # same keys and nested under "s3"
    assert organic["s3"].keys() == synthetic["s3"].keys()
    assert organic["s3"]["bucket"].keys() == synthetic["s3"]["bucket"].keys()
    # same value types under S3 (values might differ and that's OK)
    assert {type(v) for v in organic["s3"].values()} == \
        {type(v) for v in synthetic["s3"].values()}
    # spot checks for overridden properties
    # size absent on delete
    if "size" in organic["s3"]["bucket"]:
        assert organic["s3"]["object"]["size"] == synthetic["s3"]["object"]["size"]
    # versionId absent when unversioned bucket or hard delete
    if "versionId" in organic["s3"]["bucket"]:
        assert organic["s3"]["object"]["versionId"] == synthetic["s3"]["object"]["versionId"]
    # should always be present
    assert organic["awsRegion"] == synthetic["awsRegion"]
    assert organic["s3"]["bucket"]["arn"] == synthetic["s3"]["bucket"]["arn"]
    assert organic["s3"]["bucket"]["name"] == synthetic["s3"]["bucket"]["name"]
    assert organic["s3"]["object"]["key"] == synthetic["s3"]["object"]["key"]
    assert organic["s3"]["object"]["eTag"] == synthetic["s3"]["object"]["eTag"]


def make_event(
        name,
        *,
        bucket="test-bucket",
        eTag="123456",
        key="hello+world.txt",
        region="us-east-1",
        size=100,
        versionId="1313131313131.Vier50HdNbi7ZirO65",
        bucket_versioning=True
):
    """return an event based on EVENT_CORE, add fields to match organic AWS events"""
    if name in CREATE_EVENT_TYPES:
        args = {
            "bucket": bucket,
            "eTag": eTag,
            "key": key,
            "region": region,
            "size": size
        }
        if bucket_versioning:
            args["versionId"] = versionId
        return _make_event(
            name,
            **args
        )
    # no versionId or eTag in this case
    elif name == "ObjectRemoved:Delete":
        return _make_event(
            name,
            bucket=bucket,
            key=key,
            region=region
        )
    elif name == "ObjectRemoved:DeleteMarkerCreated":
        # these events are possible in both versioned and unversioned buckets
        # (e.g. bucket now unversioned that was versioned will generate a
        # delete marker on `aws s3 rm`)
        args = {
            "bucket": bucket,
            "eTag": eTag,
            "key": key,
            "region": region,
            "size": size
        }
        if bucket_versioning:
            args["versionId"] = versionId
        return _make_event(
            name,
            **args
        )
    elif name == UNKNOWN_EVENT_TYPE:
        return _make_event(UNKNOWN_EVENT_TYPE)

    else:
        raise ValueError(f"Unexpected event type: {name}")


def _make_event(
        name,
        *,
        bucket="",
        eTag="",
        key="",
        region="",
        size=0,
        versionId=""
):
    """make events in the pattern of
    https://docs.aws.amazon.com/AmazonS3/latest/dev/notification-content-structure.html
    and
    AWS Lambda > Console > Test Event
    """
    e = deepcopy(EVENT_CORE)
    e["eventName"] = name

    if bucket:
        e["s3"]["bucket"]["name"] = bucket
        e["s3"]["bucket"]["arn"] = f"arn:aws:s3:::{bucket}"
    if key:
        e["s3"]["object"]["key"] = key
    if eTag:
        e["s3"]["object"]["eTag"] = eTag
    if size:
        e["s3"]["object"]["size"] = size
    if region:
        e["awsRegion"] = region
    if versionId:
        e["s3"]["object"]["versionId"] = versionId

    return e


class MockContext():
    def get_remaining_time_in_millis(self):
        return 30000


class TestIndex(TestCase):
    def setUp(self):
        # total number of times we expect the ES _bulk API is called
        # we do not use `len(responses.calls)` because it always evaluates to 0
        # during both setup and teardown; reason is that we are using add_callback()?
        self.actual_es_calls = 0
        self.requests_mock = responses.RequestsMock(assert_all_requests_are_fired=True)
        self.requests_mock.start()

        # Create a dummy S3 client that (hopefully) can't do anything.
        self.s3_client = boto3.client('s3', config=Config(signature_version=UNSIGNED))

        self.s3_client_patcher = patch(
            __name__ + '.index.make_s3_client',
            return_value=self.s3_client
        )
        self.s3_client_patcher.start()

        self.s3_stubber = Stubber(self.s3_client)
        self.s3_stubber.activate()

        self.env_patcher = patch.dict(os.environ, {
            'ES_HOST': 'example.com',
            'AWS_ACCESS_KEY_ID': 'test_key',
            'AWS_SECRET_ACCESS_KEY': 'test_secret',
            'AWS_DEFAULT_REGION': 'ng-north-1',
        })
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

        self.s3_stubber.assert_no_pending_responses()
        self.s3_stubber.deactivate()
        self.s3_client_patcher.stop()

        self.requests_mock.stop()

    def _get_contents(self, name, ext):
        return index.get_contents(
            'test-bucket', name, ext,
            etag='etag', version_id=None, s3_client=self.s3_client, size=123,
        )

    def _make_es_callback(
            self,
            *,
            errors=False,
            status=200,
            unknown_items=False
    ):
        """
        create a callback that checks the shape of the response
        TODO: handle errors and delete actions
        """
        def check_response(request):
            raw = [json.loads(line) for line in request.body.splitlines()]
            # drop the optional source and isolate the actions
            # see https://www.elastic.co/guide/en/elasticsearch/reference/6.7/docs-bulk.html
            actions = [line for line in raw if len(line.keys()) == 1]
            items = [
                {
                    top_key: {
                        "_id": values["_id"],
                        "_index": values["_index"],
                        "_type": "_doc",
                        "status": 200
                    }
                }
                for action in actions
                for top_key, values in action.items()
            ]
            if unknown_items:
                items = [
                    {"event_we_never_heard_of": value}
                    for item in items
                    for value in item.values()
                ]
            # see https://www.elastic.co/guide/en/elasticsearch/reference/6.7/docs-bulk.html
            # for response format
            response = {
                "took": 5*len(actions),
                "errors": errors,
                "items": items
            }
            self.actual_es_calls = self.actual_es_calls + 1

            return (status, {}, json.dumps(response))

        return check_response

    def _test_index_events(
            self,
            event_names,
            *,
            bucket_versioning=True,
            errors=False,
            expected_es_calls=0,
            mock_elastic=True,
            mock_overrides=None,
            status=200,
            unknown_items=False
    ):
        """
        Reusable helper function to test indexing files based on on or more
        events
        """
        inner_records = []
        for name in event_names:
            event = make_event(name, bucket_versioning=bucket_versioning)
            inner_records.append(event)
            now = index.now_like_boto3()
            un_key = unquote_plus(event["s3"]["object"]["key"])
            eTag = event["s3"]["object"].get("eTag")
            versionId = event["s3"]["object"].get("versionId")

            expected_params = {
                'Bucket': event["s3"]["bucket"]["name"],
                'Key': un_key,
            }
            # We only get versionId for certain events (when bucket versioning is
            # on (or was on and a delete-object is issued with a particular version-id?)
            if versionId:
                expected_params["VersionId"] = versionId
            elif eTag:
                expected_params["IfMatch"] = eTag
            # infer mock status (we only talk to S3 on create events)
            mock_head = name in CREATE_EVENT_TYPES
            mock_object = name in CREATE_EVENT_TYPES
            # check for occasional overrides (which can be false)
            if mock_overrides and "mock_head" in mock_overrides:
                mock_head = mock_overrides.get("mock_head")
            if mock_overrides and "mock_object" in mock_overrides:
                mock_object = mock_overrides.get("mock_object")

            if mock_head:
                self.s3_stubber.add_response(
                    method='head_object',
                    service_response={
                        'Metadata': {},
                        'ContentLength': event["s3"]["object"]["size"],
                        'LastModified': now,
                    },
                    expected_params=expected_params
                )

            if mock_object:
                self.s3_stubber.add_response(
                    method='get_object',
                    service_response={
                        'Metadata': {},
                        'ContentLength': event["s3"]["object"]["size"],
                        'LastModified': now,
                        'Body': BytesIO(b'Hello World!'),
                    },
                    expected_params={
                        **expected_params,
                        'Range': f'bytes=0-{index.ELASTIC_LIMIT_BYTES}',
                    }
                )

        if mock_elastic:
            self.requests_mock.add_callback(
                responses.POST,
                'https://example.com:443/_bulk',
                callback=self._make_es_callback(
                    errors=errors,
                    status=status,
                    unknown_items=unknown_items
                ),
                content_type='application/json'
            )

        records = {
            "Records": [{
                "body": json.dumps({
                    "Message": json.dumps({
                        "Records": inner_records
                    })
                })
            }]
        }

        index.handler(records, MockContext())
        assert self.actual_es_calls == expected_es_calls, \
            (
                f"Expected ES endpoint to be called {expected_es_calls} times, "
                "got {self.expected_es_calls} calls instead"
            )

    def test_create_event_failure(self):
        """
        Check that the indexer doesn't blow up on create event failures.
        """
        with pytest.raises(RetryError, match="Failed to load"):
            self._test_index_events(
                ["ObjectCreated:Put"],
                errors=True,
                status=400
            )

    def test_create_copy_index(self):
        """test indexing a single file from copy event"""
        self._test_index_events(
            ["ObjectCreated:Copy"],
            expected_es_calls=1
        )
        # Elastic only needs to be mocked once per test

    def test_create_put_index(self):
        """test indexing a single file from put event"""
        self._test_index_events(
            ["ObjectCreated:Put"],
            expected_es_calls=1
        )

    def test_create_put_index_unversioned(self):
        """test indexing a single file from put event"""
        self._test_index_events(
            ["ObjectCreated:Put"],
            bucket_versioning=False,
            expected_es_calls=1
        )

    def test_create_post_index(self):
        """test indexing a single file from post event"""
        self._test_index_events(
            ["ObjectCreated:Post"],
            expected_es_calls=1
        )

    def test_create_multipart_index(self):
        """test indexing a single file from post event"""
        self._test_index_events(
            ["ObjectCreated:CompleteMultipartUpload"],
            expected_es_calls=1
        )

    def test_delete_event(self):
        """
        Check that the indexer doesn't blow up on delete events.
        """
        self._test_index_events(
            ["ObjectRemoved:Delete"],
            expected_es_calls=1
        )

    def test_delete_event_failure(self):
        """
        Check that the indexer doesn't blow up on delete event failures.
        """
        # TODO, why does pytest.raises(RetryError not work?)
        with pytest.raises(RetryError, match="Failed to load"):
            self._test_index_events(
                ["ObjectRemoved:Delete"],
                errors=True,
                status=400
            )

    def test_delete_event_no_versioning(self):
        """
        Check that the indexer doesn't blow up on delete events.
        """
        self._test_index_events(
            ["ObjectRemoved:Delete"],
            bucket_versioning=False,
            expected_es_calls=1
        )

    def test_delete_marker_event(self):
        """
        common event in versioned; buckets, should no-op
        """
        self._test_index_events(
            ["ObjectRemoved:DeleteMarkerCreated"],
            # we should never call elastic in this case
            mock_elastic=False
        )

    def test_delete_marker_event_no_versioning(self):
        """
        this can happen if a bucket was verisoned, and now isn't, followed by
        `aws s3 rm`
        """
        # don't mock head or get; this event should never call them
        self._test_index_events(
            ["ObjectRemoved:DeleteMarkerCreated"],
            bucket_versioning=False,
            expected_es_calls=1
        )

    @patch(__name__ + '.index.get_contents')
    def test_index_exception(self, get_mock):
        """test indexing a single file that throws an exception"""
        class ContentException(Exception):
            pass
        get_mock.side_effect = ContentException("Unable to get contents")
        with pytest.raises(ContentException):
            # get_mock already mocks get_object, so don't mock it in _test_index_event
            self._test_index_events(
                ["ObjectCreated:Put"],
                mock_overrides={
                    "mock_object": False
                }
            )

    def test_infer_extensions(self):
        """ensure we are guessing file types well"""
        # parquet
        assert index.infer_extensions("s3/some/file.c000", ".c000") == ".parquet", \
            "Expected .c0000 to infer as .parquet"
        # parquet, nonzero part number
        assert index.infer_extensions("s3/some/file.c001", ".c001") == ".parquet", \
            "Expected .c0001 to infer as .parquet"
        # -c0001 file
        assert index.infer_extensions("s3/some/file-c0001", "") == ".parquet", \
            "Expected -c0001 to infer as .parquet"
        # -c00111 file (should never happen)
        assert index.infer_extensions("s3/some/file-c000121", "") == "", \
            "Expected -c000121 not to infer as .parquet"
        # .txt file, should be unchanged
        assert index.infer_extensions("s3/some/file-c0000.txt", ".txt") == ".txt", \
            "Expected .txt to infer as .txt"

    def test_multiple_index_events(self):
        """
        Messages from SQS contain up to N messages.
        Currently N=10, this number is determined on the backend by CloudFormation
        """
        self._test_index_events(
            [
                "ObjectCreated:Put",
                "ObjectCreated:Put",
                "ObjectCreated:Put",
                "ObjectCreated:Put",
                "ObjectCreated:Put",
                "ObjectCreated:Copy",
                "ObjectCreated:Copy",
                "ObjectCreated:Copy",
                "ObjectCreated:Copy",
                "ObjectRemoved:Delete"
            ],
            expected_es_calls=1
        )

    def test_extension_overrides(self):
        """ensure that only the file extensions in override are indexed"""
        with patch(__name__ + '.index.CONTENT_INDEX_EXTS', {'.unique1', '.unique2'}):
            self.s3_stubber.add_response(
                method='get_object',
                service_response={
                    'Metadata': {},
                    'ContentLength': 123,
                    'Body': BytesIO(b'Hello World!'),
                },
                expected_params={
                    'Bucket': 'test-bucket',
                    'Key': 'foo.unique1',
                    'IfMatch': 'etag',
                    'Range': f'bytes=0-{index.ELASTIC_LIMIT_BYTES}',
                }
            )
            self.s3_stubber.add_response(
                method='get_object',
                service_response={
                    'Metadata': {},
                    'ContentLength': 123,
                    'Body': BytesIO(b'Hello World!'),
                },
                expected_params={
                    'Bucket': 'test-bucket',
                    'Key': 'foo.unique2',
                    'IfMatch': 'etag',
                    'Range': f'bytes=0-{index.ELASTIC_LIMIT_BYTES}',
                }
            )
            # only these two file types should be indexed
            assert self._get_contents('foo.unique1', '.unique1') == "Hello World!"
            assert self._get_contents('foo.unique2', '.unique2') == "Hello World!"
            # these files should not get content indexed, therefore no S3 mock
            assert self._get_contents('foo.txt', '.txt') == ""
            assert self._get_contents('foo.ipynb', '.ipynb') == ""

    def test_synthetic_copy_event(self):
        """check synthetic ObjectCreated:Copy event vs organic obtained on 26-May-2020
        (bucket versioning on)
        """
        synthetic = make_event(
            "ObjectCreated:Copy",
            bucket="somebucket",
            key="events/copy-one/0.png",
            size=73499,
            eTag="7b4b71116bb21d3ea7138dfe7aabf036",
            region="us-west-1",
            versionId="Yj1vyLWcE9FTFIIrsgk.yAX7NbJrAW7g"
        )
        # actual event from S3 with a few obfuscations to protect the innocent
        organic = {
            "eventVersion": "2.1",
            "eventSource": "aws:s3",
            "awsRegion": "us-west-1",
            "eventTime": "2020-05-26T22:15:10.906Z",
            "eventName": "ObjectCreated:Copy",
            "userIdentity": {
                "principalId": "AWS:EXAMPLEDUDE"
            },
            "requestParameters": {
                "sourceIPAddress": "12.999.99.999"
            },
            "responseElements": {
                "x-amz-request-id": "CEF0E4FD6D0944D7",
                "x-amz-id-2": "EXAMPLE/+GUID/m/HAApWP+3arsz0QPph7OBVdl1"
            },
            "s3": {
                "s3SchemaVersion": "1.0",
                "configurationId": "YmJkYWUyYmYtNzg5OC00NGRiLTk0NmItNDMxNzA4NzhiZDNk",
                "bucket": {
                    "name": "somebucket",
                    "ownerIdentity": {
                        "principalId": "SAMPLE"
                    },
                    "arn": "arn:aws:s3:::somebucket"
                },
                "object": {
                    "key": "events/copy-one/0.png",
                    "size": 73499,
                    "eTag": "7b4b71116bb21d3ea7138dfe7aabf036",
                    "versionId": "Yj1vyLWcE9FTFIIrsgk.yAX7NbJrAW7g",
                    "sequencer": "005ECD94EFA9B09DD8"
                }
            }
        }
        _check_event(synthetic, organic)

    def test_synthetic_copy_event_no_versioning(self):
        """check synthetic ObjectCreated:Copy event vs organic obtained on 26-May-2020
        (this is for a bucket with versioning turned off)
        """
        synthetic = make_event(
            "ObjectCreated:Copy",
            bucket="somebucket",
            key="events/copy-one-noversioning/0.png",
            size=73499,
            eTag="7b4b71116bb21d3ea7138dfe7aabf036",
            region="us-west-1",
        )
        # actual event from S3 with a few obfuscations to protect the innocent
        organic = {
            "eventVersion": "2.1",
            "eventSource": "aws:s3",
            "awsRegion": "us-west-1",
            "eventTime": "2020-05-27T20:31:45.823Z",
            "eventName": "ObjectCreated:Copy",
            "userIdentity": {"principalId": "AWS:boombomakasdfsdf"},
            "requestParameters": {"sourceIPAddress": "07.123.45.899"},
            "responseElements": {
                "x-amz-request-id": "DECF307B5F55C78D",
                "x-amz-id-2": "guid/hash/tG++guid/stuff"
            },
            "s3": {
                "s3SchemaVersion": "1.0",
                "configurationId": "stuff",
                "bucket": {
                    "name": "somebucket",
                    "ownerIdentity": {"principalId": "B3ASKDFASDFAF"},
                    "arn": "arn:aws:s3:::somebucket"},
                "object": {
                    "key": "events/copy-one-noversioning/0.png",
                    "size": 73499,
                    "eTag": "7b4b71116bb21d3ea7138dfe7aabf036",
                    "sequencer": "005ECECE336C7A4715"
                }
            }
        }
        _check_event(synthetic, organic)

    def test_synthetic_delete_marker_event_no_versioning(self):
        """check synthetic ObjectRemoved:DeleteMarkerCreated event vs organic
        obtained on 27-May-2020
        (this is for a bucket that had versioning on but now off)
        note: this is a standard delete marker minus the versionId
        """
        synthetic = make_event(
            "ObjectRemoved:DeleteMarkerCreated",
            key="events/copy-many-noversioning/0.png",
            eTag="d41d8cd98f00b204e9800998ecf8427e",
            region="us-west-1",
        )
        # actual event from S3 with a few obfuscations to protect the innocent
        organic = {
            "eventVersion": "2.1",
            "eventSource": "aws:s3",
            "awsRegion": "us-west-1",
            "eventTime": "2020-05-28T23:53:24.662Z",
            "eventName": "ObjectRemoved:DeleteMarkerCreated",
            "userIdentity": {"principalId": "AWS:boommasdfagnag"},
            "requestParameters": {"sourceIPAddress": "12.888.91.910"},
            "responseElements": {
                "x-amz-request-id": "35781DEB9DA7612E",
                "x-amz-id-2": "Qguid+Oguid+WRa/guid/guid+AwtLbBepO7QEBNbwguid/LfQguid"
            },
            "s3": {
                "s3SchemaVersion": "1.0",
                "configurationId": "guiadskfjasdlfkjasdklfjasdfd",
                "bucket": {
                    "name": "test-bucket",
                    "ownerIdentity": {"principalId": "adflkjasdklfjadf"},
                    "arn": "arn:aws:s3:::test-bucket"
                },
                "object": {
                    "key": "events/copy-many-noversioning/0.png",
                    "eTag": "d41d8cd98f00b204e9800998ecf8427e",
                    "sequencer": "005ED04EF537DAB0EE"
                }
            }
        }
        _check_event(synthetic, organic)

    def test_synthetic_put_event(self):
        """check synthetic ObjectCreated:Put event vs organic obtained on 27-May-2020
        (bucket versioning on)"""
        synthetic = make_event(
            "ObjectCreated:Copy",
            bucket="anybucket",
            key="events/put-one/storms.parquet",
            size=923078,
            eTag="502f21cfc143fb0c35f563eda5699fa9",
            region="us-west-1",
            versionId="yYSoQSg3.BfosdUxnRSv9vFg.WAPMmfn"
        )
        # actual event from S3 with a few obfuscations to protect the innocent
        organic = {
            "eventVersion": "2.1",
            "eventSource": "aws:s3",
            "awsRegion": "us-west-1",
            "eventTime": "2020-05-27T18:57:36.268Z",
            "eventName": "ObjectCreated:Put",
            "userIdentity": {"principalId": "AWS:notgonnabehereanyway"},
            "requestParameters": {"sourceIPAddress": "12.345.67.890"},
            "responseElements": {
                "x-amz-request-id": "371A83BCE4341D7D",
                "x-amz-id-2": "a+example+morestuff+01343413434234234234"
            },
            "s3": {
                "s3SchemaVersion": "1.0",
                "configurationId": "YmJkYWUyYmYtNzg5OC00NGRiLTk0NmItNDMxNzA4NzhiZDNk",
                "bucket": {
                    "name": "anybucket",
                    "ownerIdentity": {"principalId": "myidhere"},
                    "arn": "arn:aws:s3:::anybucket"
                },
                "object": {
                    "key": "events/put-one/storms.parquet",
                    "size": 923078,
                    "eTag": "502f21cfc143fb0c35f563eda5699fa9",
                    "versionId": "yYSoQSg3.BfosdUxnRSv9vFg.WAPMmfn",
                    "sequencer": "005ECEB81C34962CFC"
                }
            }
        }
        _check_event(synthetic, organic)

    def test_synthetic_multipart_event(self):
        """check synthetic ObjectCreated:Put event vs organic obtained on 27-May-2020
        (bucket versioning on)"""
        synthetic = make_event(
            "ObjectCreated:CompleteMultipartUpload",
            bucket="anybucket",
            key="events/multipart-one/part-00006-495c48e6-96d6-4650-aa65-3c36a3516ddd.c000.snappy.parquet",
            size=135397292,
            eTag="0eb149127d0277326dedcf0c530ca966-17",
            region="us-west-1",
            versionId="bKufwe3zvJ3SQn3F9Z.akBkenOYl_SIz"
        )
        # actual event from S3 with a few obfuscations to protect the innocent
        organic = {
            "eventVersion": "2.1",
            "eventSource": "aws:s3",
            "awsRegion": "us-west-1",
            "eventTime": "2020-05-27T20:13:18.791Z",
            "eventName": "ObjectCreated:CompleteMultipartUpload",
            "userIdentity": {"principalId": "AWS:nogonnabehere"},
            "requestParameters": {"sourceIPAddress": "12.999.88.131"},
            "responseElements": {
                "x-amz-request-id": "9F9BABC04A681C48",
                "x-amz-id-2": "Lfs+guid/anotherguid"
            },
            "s3": {
                "s3SchemaVersion": "1.0",
                "configurationId": "XSASFASFASDFASFASFYGUID",
                "bucket": {
                    "name": "searchminimal",
                    "ownerIdentity": {"principalId": "A13432523432423535"},
                    "arn": "arn:aws:s3:::searchminimal"
                },
                "object": {
                    "key": "events/multipart-one/part-00006-495c48e6-96d6-4650-aa65-3c36a3516ddd.c000.snappy.parquet",
                    "size": 135397292,
                    "eTag": "0eb149127d0277326dedcf0c530ca966-17",
                    "versionId": "bKufwe3zvJ3SQn3F9Z.akBkenOYl_SIz",
                    "sequencer": "005ECEC96FC6B634D7"
                }
            }
        }

    def test_test_event(self):
        """
        Check that the indexer does not barf when it gets an S3 test notification.
        """
        event = {
            "Records": [{
                "body": json.dumps({
                    "Message": json.dumps({
                        "Service": "Amazon S3",
                        "Event": "s3:TestEvent",
                        "Time": "2014-10-13T15:57:02.089Z",
                        "Bucket": "test-bucket",
                        "RequestId": "5582815E1AEA5ADF",
                        "HostId": "fakeGUIDhere+YstdA6Knx4Ip8EXAMPLE"
                    })
                })
            }]
        }

        index.handler(event, None)

    def test_unexpected_event(self):
        """
        Test unknown event types
        """
        # the indexer should just pass over this event without touching S3 or ES
        self._test_index_events([UNKNOWN_EVENT_TYPE], mock_elastic=False)

    def test_unknown_event_failure(self):
        """
        send unrecognizable error keys back from ES
        """
        with pytest.raises(RetryError, match="Failed to load"):
            # we don't set expected_es_calls here because the assert is never hit
            # because of the exceptions, but we do check it directly
            self._test_index_events(
                ["ObjectCreated:Put"],
                errors=True,
                status=400,
                unknown_items=True
            )
        assert self.actual_es_calls == 2, "Two failures should have called _bulk twice"

    def test_unsupported_contents(self):
        assert self._get_contents('foo.exe', '.exe') == ""
        assert self._get_contents('foo.exe.gz', '.exe.gz') == ""

    def test_get_plain_text(self):
        self.s3_stubber.add_response(
            method='get_object',
            service_response={
                'Metadata': {},
                'ContentLength': 123,
                'Body': BytesIO(b'Hello World!\nThere is more to know.'),
            },
            expected_params={
                'Bucket': 'test-bucket',
                'Key': 'foo.txt',
                'IfMatch': 'etag',
                'Range': f'bytes=0-{index.ELASTIC_LIMIT_BYTES}',
            }
        )

        contents = index.get_plain_text(
            'test-bucket',
            'foo.txt',
            compression=None,
            etag='etag',
            version_id=None,
            s3_client=self.s3_client,
            size=123
        )
        assert contents == "Hello World!\nThere is more to know."

    def test_text_contents(self):
        self.s3_stubber.add_response(
            method='get_object',
            service_response={
                'Metadata': {},
                'ContentLength': 123,
                'Body': BytesIO(b'Hello World!'),
            },
            expected_params={
                'Bucket': 'test-bucket',
                'Key': 'foo.txt',
                'IfMatch': 'etag',
                'Range': f'bytes=0-{index.ELASTIC_LIMIT_BYTES}',
            }
        )

        assert self._get_contents('foo.txt', '.txt') == "Hello World!"

    def test_gzipped_text_contents(self):
        self.s3_stubber.add_response(
            method='get_object',
            service_response={
                'Metadata': {},
                'ContentLength': 123,
                'Body': BytesIO(compress(b'Hello World!')),
            },
            expected_params={
                'Bucket': 'test-bucket',
                'Key': 'foo.txt.gz',
                'IfMatch': 'etag',
                'Range': f'bytes=0-{index.ELASTIC_LIMIT_BYTES}',
            }
        )

        assert self._get_contents('foo.txt.gz', '.txt.gz') == "Hello World!"

    def test_notebook_contents(self):
        notebook = (BASE_DIR / 'normal.ipynb').read_bytes()

        self.s3_stubber.add_response(
            method='get_object',
            service_response={
                'Metadata': {},
                'ContentLength': 123,
                'Body': BytesIO(notebook),
            },
            expected_params={
                'Bucket': 'test-bucket',
                'Key': 'foo.ipynb',
                'IfMatch': 'etag',
            }
        )

        assert "model.fit" in self._get_contents('foo.ipynb', '.ipynb')

    def test_gzipped_notebook_contents(self):
        notebook = compress((BASE_DIR / 'normal.ipynb').read_bytes())

        self.s3_stubber.add_response(
            method='get_object',
            service_response={
                'Metadata': {},
                'ContentLength': 123,
                'Body': BytesIO(notebook),
            },
            expected_params={
                'Bucket': 'test-bucket',
                'Key': 'foo.ipynb.gz',
                'IfMatch': 'etag',
            }
        )

        assert "Model results visualization" in self._get_contents('foo.ipynb.gz', '.ipynb.gz')

    def test_parquet_contents(self):
        parquet = (BASE_DIR / 'amazon-reviews-1000.snappy.parquet').read_bytes()
        self.s3_stubber.add_response(
            method='get_object',
            service_response={
                'Metadata': {},
                'ContentLength': 123,
                'Body': BytesIO(parquet),
            },
            expected_params={
                'Bucket': 'test-bucket',
                'Key': 'foo.parquet',
                'IfMatch': 'etag',
            }
        )

        contents = self._get_contents('foo.parquet', '.parquet')
        size = len(contents.encode('utf-8', 'ignore'))
        assert size <= index.ELASTIC_LIMIT_BYTES
        # spot check for contents
        assert "This is not even worth the money." in contents
        assert "As for results; I felt relief almost immediately." in contents
        assert "R2LO11IPLTDQDX" in contents

    # see PRE conditions in conftest.py
    @pytest.mark.extended
    def test_parquet_extended(self):
        directory = (BASE_DIR / 'amazon-reviews-pds')
        files = directory.glob('**/*.parquet')
        for f in files:
            print(f"Testing {f}")
            parquet = f.read_bytes()

            self.s3_stubber.add_response(
                method='get_object',
                service_response={
                    'Metadata': {},
                    'ContentLength': 123,
                    'Body': BytesIO(parquet),
                },
                expected_params={
                    'Bucket': 'test-bucket',
                    'Key': 'foo.parquet',
                    'IfMatch': 'etag',
                }
            )
