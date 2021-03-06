""" core logic for fetching documents from S3 and queueing them locally before
sending to elastic search in memory-limited batches"""
from datetime import datetime
from math import floor
import os

from aws_requests_auth.aws_auth import AWSRequestsAuth
import boto3
from elasticsearch import Elasticsearch, RequestsHttpConnection
from elasticsearch.helpers import bulk

from t4_lambda_shared.utils import separated_env_to_iter
from t4_lambda_shared.preview import ELASTIC_LIMIT_BYTES


CONTENT_INDEX_EXTS = separated_env_to_iter("CONTENT_INDEX_EXTS") or {
    ".csv",
    ".ipynb",
    ".json",
    ".md",
    ".parquet",
    ".rmd",
    ".tsv",
    ".txt"
}

EVENT_PREFIX = {
    "Created": "ObjectCreated:",
    "Removed": "ObjectRemoved:"
}

# See https://amzn.to/2xJpngN for chunk size as a function of container size
CHUNK_LIMIT_BYTES = int(os.getenv('CHUNK_LIMIT_BYTES') or 9_500_000)
ELASTIC_TIMEOUT = 30
MAX_BACKOFF = 360  # seconds
MAX_RETRY = 4  # prevent long-running lambdas due to malformed calls
# signifies that the object is truly deleted, not to be confused with
# s3:ObjectRemoved:DeleteMarkerCreated, which we may see in versioned buckets
# see https://docs.aws.amazon.com/AmazonS3/latest/dev/NotificationHowTo.html
QUEUE_LIMIT_BYTES = 100_000_000  # 100MB
RETRY_429 = 5


# pylint: disable=super-init-not-called
class RetryError(Exception):
    """Fatal and final error if docs fail after multiple retries"""
    def __init__(self, message):
        pass


class DocumentQueue:
    """transient in-memory queue for documents to be indexed"""
    def __init__(self, context):
        """constructor"""
        self.queue = []
        self.size = 0
        self.context = context

    def append(
            self,
            event_type,
            size=0,
            *,
            last_modified,
            bucket,
            ext,
            key,
            text,
            etag,
            version_id
    ):
        """format event as a document and then queue the document"""
        if not isinstance(version_id, (str, type(None))):
            # should never raise given how index.py, the only caller of .append(),
            # works
            raise ValueError(
                f".append() must set version_id even if missing from event; "
                f"got {version_id}"
            )
        if event_type.startswith(EVENT_PREFIX["Created"]):
            _op_type = "index"
        elif event_type.startswith(EVENT_PREFIX["Removed"]):
            _op_type = "delete"
        else:
            print("Skipping unrecognized event type {event_type}")
            return
        # On types and fields, see
        # https://www.elastic.co/guide/en/elasticsearch/reference/master/mapping.html
        body = {
            # Elastic native keys
            "_id": f"{key}:{version_id}",
            "_index": bucket,
            "_type": "_doc",
            # index will upsert (and clobber existing equivalent _ids)
            "_op_type": "delete" if event_type.startswith(EVENT_PREFIX["Removed"]) else "index",
            # Quilt keys
            # Be VERY CAREFUL changing these values, as a type change can cause a
            # mapper_parsing_exception that below code won't handle
            # TODO: remove this field from ES in /enterprise (now deprecated and unused)
            "comment": "",
            "content": text,  # field for full-text search
            "etag": etag,
            "event": event_type,
            "ext": ext,
            "key": key,
            # "key_text": created by mappings copy_to
            "last_modified": last_modified.isoformat(),
            # TODO: remove this field from ES in /enterprise (now deprecated and unused)
            "meta_text": "",
            "size": size,
            "target": "",
            "updated": datetime.utcnow().isoformat(),
            "version_id": version_id
        }

        self.append_document(body)

        if self.size >= QUEUE_LIMIT_BYTES:
            self.send_all()

    def append_document(self, doc):
        """append well-formed documents (used for retry or by append())"""
        if doc["content"]:
            # document text dominates memory footprint; OK to neglect the
            # small fixed size for the JSON metadata
            self.size += min(doc["size"], ELASTIC_LIMIT_BYTES)
        self.queue.append(doc)

    def send_all(self):
        """flush self.queue in 1-2 bulk calls"""
        if not self.queue:
            return
        elastic_host = os.environ["ES_HOST"]
        session = boto3.session.Session()
        credentials = session.get_credentials().get_frozen_credentials()
        awsauth = AWSRequestsAuth(
            # These environment variables are automatically set by Lambda
            aws_access_key=credentials.access_key,
            aws_secret_access_key=credentials.secret_key,
            aws_token=credentials.token,
            aws_host=elastic_host,
            aws_region=session.region_name,
            aws_service="es"
        )

        elastic = Elasticsearch(
            hosts=[{"host": elastic_host, "port": 443}],
            http_auth=awsauth,
            max_backoff=get_time_remaining(self.context) if self.context else MAX_BACKOFF,
            # Give ES time to respond when under load
            timeout=ELASTIC_TIMEOUT,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection
        )
        # For response format see
        # https://www.elastic.co/guide/en/elasticsearch/reference/6.7/docs-bulk.html
        # (We currently use Elastic 6.7 per quiltdata/deployment search.py)
        # note that `elasticsearch` post-processes this response
        _, errors = bulk_send(elastic, self.queue)
        if errors:
            id_to_doc = {d["_id"]: d for d in self.queue}
            send_again = []
            for error in errors:
                # retry index and delete errors
                if "index" in error or "delete" in error:
                    if "index" in error:
                        inner = error["index"]
                    if "delete" in error:
                        inner = error["delete"]
                    if "_id" in inner:
                        doc = id_to_doc[inner["_id"]]
                        # Always retry the source document if we can identify it.
                        # This catches temporary 403 on index write blocks & other
                        # transient issues.
                        send_again.append(doc)
                # retry the entire batch
                else:
                    # Unclear what would cause an error that's neither index nor delete
                    # but if there's an unknown error we need to assume it applies to
                    # the batch.
                    send_again = self.queue
            # Last retry (though elasticsearch might retry 429s tho)
            if send_again:
                _, errors = bulk_send(elastic, send_again)
                if errors:
                    raise RetryError(
                        "Failed to load messages into Elastic on second retry.\n"
                        f"{_}\nErrors: {errors}\nTo resend:{send_again}"
                    )
        # empty the queue
        self.size = 0
        self.queue = []


def get_time_remaining(context):
    """returns time remaining in seconds before lambda context is shut down"""
    time_remaining = floor(context.get_remaining_time_in_millis()/1000)
    if time_remaining < 30:
        print(
            f"Warning: Lambda function has less than {time_remaining} seconds."
            " Consider reducing bulk batch size."
        )

    return time_remaining


def bulk_send(elastic, list_):
    """make a bulk() call to elastic"""
    return bulk(
        elastic,
        list_,
        # Some magic numbers to reduce memory pressure
        # e.g. see https://github.com/wagtail/wagtail/issues/4554
        chunk_size=100,  # max number of documents sent in one chunk
        # The stated default is max_chunk_bytes=10485760, but with default
        # ES will still return an exception stating that the very
        # same request size limit has been exceeded
        max_chunk_bytes=CHUNK_LIMIT_BYTES,
        # number of retries for 429 (too many requests only)
        # all other errors handled by our code
        max_retries=RETRY_429,
        # we'll process errors on our own
        raise_on_error=False,
        raise_on_exception=False
    )
