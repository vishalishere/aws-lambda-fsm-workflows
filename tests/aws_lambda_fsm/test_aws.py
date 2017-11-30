# Copyright 2016 Workiva Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# system imports
import unittest

# library imports
import mock
from botocore.exceptions import ClientError
import memcache
import redis

# application imports
from aws_lambda_fsm.constants import AWS
from aws_lambda_fsm.constants import AWS_SQS
from aws_lambda_fsm.constants import AWS_ELASTICACHE
from aws_lambda_fsm.aws import get_connection
from aws_lambda_fsm.aws import retriable_entities
from aws_lambda_fsm.aws import store_checkpoint
from aws_lambda_fsm.aws import store_environment
from aws_lambda_fsm.aws import load_environment
from aws_lambda_fsm.aws import start_retries
from aws_lambda_fsm.aws import stop_retries
from aws_lambda_fsm.aws import send_next_event_for_dispatch
from aws_lambda_fsm.aws import send_next_events_for_dispatch
from aws_lambda_fsm.aws import set_message_dispatched
from aws_lambda_fsm.aws import get_message_dispatched
from aws_lambda_fsm.aws import increment_error_counters
from aws_lambda_fsm.aws import get_primary_stream_source
from aws_lambda_fsm.aws import get_secondary_stream_source
from aws_lambda_fsm.aws import get_primary_environment_source
from aws_lambda_fsm.aws import get_secondary_environment_source
from aws_lambda_fsm.aws import get_primary_checkpoint_source
from aws_lambda_fsm.aws import get_secondary_checkpoint_source
from aws_lambda_fsm.aws import _local
from aws_lambda_fsm.aws import _get_service_connection
from aws_lambda_fsm.aws import _get_connection_info
from aws_lambda_fsm.aws import _get_sqs_queue_url
from aws_lambda_fsm.aws import _get_elasticache_engine_and_endpoint
from aws_lambda_fsm.aws import ChaosConnection
from aws_lambda_fsm.aws import get_arn_from_arn_string
from aws_lambda_fsm.aws import _validate_config
from aws_lambda_fsm.aws import _validate_sqs_urls
from aws_lambda_fsm.aws import _validate_elasticache_endpoints
from aws_lambda_fsm.aws import validate_config
from aws_lambda_fsm.aws import acquire_lease
from aws_lambda_fsm.aws import release_lease


class Connection(object):
    _method_to_api_mapping = {'find_things': 'FindThingsApi'}
    called = False

    def find_things(self):
        self.called = True
        return 1

    def pipeline(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


def _get_test_arn(service, resource='resourcetype/resourcename'):
    return ':'.join(
        ['arn', 'aws', service, 'testing', '1234567890', resource]
    )


ELASTICACHE_ENDPOINTS_REDIS = {
    _get_test_arn(AWS.ELASTICACHE): {
        AWS_ELASTICACHE.Engine: AWS_ELASTICACHE.ENGINE.REDIS,
        AWS_ELASTICACHE.ConfigurationEndpoint: {
            AWS_ELASTICACHE.CONFIGURATION_ENDPOINT.Address: "localhost",
            AWS_ELASTICACHE.CONFIGURATION_ENDPOINT.Port: "12345",
        }
    }
}

ELASTICACHE_ENDPOINTS_MEMCACHE = {
    _get_test_arn(AWS.ELASTICACHE): {
        AWS_ELASTICACHE.Engine: AWS_ELASTICACHE.ENGINE.MEMCACHED,
        AWS_ELASTICACHE.ConfigurationEndpoint: {
            AWS_ELASTICACHE.CONFIGURATION_ENDPOINT.Address: "localhost",
            AWS_ELASTICACHE.CONFIGURATION_ENDPOINT.Port: "54321",
        }
    }
}

ENDPOINTS_MEMCACHE = {
    AWS.ELASTICACHE: {
        'testing': 'foobar:1234'
    }
}


class TestArn(unittest.TestCase):

    def test_slash_resource_missing(self):
        arn = get_arn_from_arn_string('')
        self.assertIsNone(arn.slash_resource())

    def test_slash_resource(self):
        arn_string = _get_test_arn(AWS.KINESIS)
        arn = get_arn_from_arn_string(arn_string)
        self.assertEqual('resourcename', arn.slash_resource())

    def test_colon_resource_missing(self):
        arn = get_arn_from_arn_string('')
        self.assertIsNone(arn.colon_resource())

    def test_colon_resource(self):
        arn_string = _get_test_arn(AWS.KINESIS, resource='foo:bar')
        arn = get_arn_from_arn_string(arn_string)
        self.assertEqual('bar', arn.colon_resource())


class TestAws(unittest.TestCase):

    def test_chaos_0(self):
        connection = Connection()
        connection = ChaosConnection('kinesis', connection, chaos={'dynamodb': {Exception(): 1.0}})
        ret = connection.find_things()
        self.assertEqual(1, ret)

    def test_chaos_0_explicit(self):
        connection = Connection()
        connection = ChaosConnection('kinesis', connection, chaos={'kinesis': {Exception(): 0.0}})
        ret = connection.find_things()
        self.assertEqual(1, ret)

    def test_chaos_100_raise(self):
        connection = Connection()
        connection = ChaosConnection('kinesis', connection, chaos={'kinesis': {Exception(): 1.0}})
        self.assertRaises(Exception, connection.find_things)

    def test_chaos_100_return(self):
        connection = Connection()
        connection = ChaosConnection('kinesis', connection, chaos={'kinesis': {'zap': 1.0}})
        ret = connection.find_things()
        self.assertEqual('zap', ret)

    @mock.patch('aws_lambda_fsm.aws.random')
    def test_chaos_run_function(self, mock_random):
        mock_random.uniform.return_value = 0.1
        connection = Connection()
        connection = ChaosConnection('kinesis', connection, chaos={'kinesis': {'zap': 1.0}})
        connection.find_things()
        self.assertTrue(connection.called)

    @mock.patch('aws_lambda_fsm.aws.random')
    def test_chaos_dont_run_function(self, mock_random):
        mock_random.uniform.return_value = 0.9
        connection = Connection()
        connection = ChaosConnection('kinesis', connection, chaos={'kinesis': {'zap': 1.0}})
        connection.find_things()
        self.assertFalse(connection.called)

    def test_chaos_redis_pipeline_context_manager(self):
        connection = Connection()
        connection = ChaosConnection('redis', connection, chaos={'redis': {'zap': 1.0}})
        with connection.pipeline() as pipe:
            ret = pipe.find_things()
            self.assertEqual('zap', ret)

    ##################################################
    # Connection Functions
    ##################################################

    # get_arn_from_arn_string

    def test_get_arn_from_arn_string(self):
        arn = get_arn_from_arn_string("a:b:c:d:e:f:g:h")
        self.assertEqual('a', arn.arn)
        self.assertEqual('b', arn.partition)
        self.assertEqual('c', arn.service)
        self.assertEqual('d', arn.region_name)
        self.assertEqual('e', arn.account_id)
        self.assertEqual('f:g:h', arn.resource)

    def test_get_arn_from_arn_string_not_long_enough(self):
        arn = get_arn_from_arn_string("a:b:c")
        self.assertEqual('a', arn.arn)
        self.assertEqual('b', arn.partition)
        self.assertEqual('c', arn.service)
        self.assertIsNone(arn.region_name)
        self.assertIsNone(arn.account_id)
        self.assertIsNone(arn.resource)

    def test_get_arn_from_arn_string_no_string_at_all(self):
        arn = get_arn_from_arn_string(None)
        self.assertIsNone(arn.arn)
        self.assertIsNone(arn.partition)
        self.assertIsNone(arn.service)
        self.assertIsNone(arn.region_name)
        self.assertIsNone(arn.account_id)
        self.assertIsNone(arn.resource)

    # _get_elasticache_engine_and_endpoint

    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_get_elasticache_engine_and_endpoint_legacy_memcache1(self,
                                                                  mock_settings):
        mock_settings.ENDPOINTS = {
            'elasticache': {'testing': 'host:1111'}}
        actual = _get_elasticache_engine_and_endpoint(_get_test_arn(AWS.ELASTICACHE))
        expected = ('memcached', 'host:1111')
        self.assertEqual(expected, actual)

    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_get_elasticache_engine_and_endpoint_legacy_memcache2(self,
                                                                  mock_settings):
        mock_settings.ENDPOINTS = {
            _get_test_arn(AWS.ELASTICACHE): 'host:2222'}
        actual = _get_elasticache_engine_and_endpoint(_get_test_arn(AWS.ELASTICACHE))
        expected = ('memcached', 'host:2222')
        self.assertEqual(expected, actual)

    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_get_elasticache_engine_and_endpoint(self,
                                                 mock_settings):
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        actual = _get_elasticache_engine_and_endpoint(_get_test_arn(AWS.ELASTICACHE))
        expected = ('redis', 'localhost:12345')
        self.assertEqual(expected, actual)

    @mock.patch('aws_lambda_fsm.aws.settings')
    @mock.patch('aws_lambda_fsm.aws.boto3')
    def test_get_elasticache_engine_and_endpoint_not_found(self,
                                                           mock_boto3,
                                                           mock_settings):
        setattr(_local, 'cache_details_for_' + _get_test_arn(AWS.ELASTICACHE), None)
        mock_boto3.client.return_value.describe_cache_clusters.return_value = {
            'CacheClusters': []
        }
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = {}
        actual = _get_elasticache_engine_and_endpoint(_get_test_arn(AWS.ELASTICACHE))
        self.assertIsNone(actual)

    @mock.patch('aws_lambda_fsm.aws.settings')
    @mock.patch('aws_lambda_fsm.aws.boto3')
    def test_get_elasticache_engine_and_endpoint_invalid(self,
                                                         mock_boto3,
                                                         mock_settings):
        setattr(_local, 'cache_details_for_' + _get_test_arn(AWS.ELASTICACHE), None)
        mock_boto3.client.return_value.describe_cache_clusters.return_value = {
            'CacheClusters': [{
                'Engine': 'memcached',
                'foobar': {
                    'Port': 11211,
                    'Address': 'foobar.cfg.cache.amazonaws.com'
                }
            }]
        }
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = {}
        actual = _get_elasticache_engine_and_endpoint(_get_test_arn(AWS.ELASTICACHE))
        self.assertIsNone(actual)

    @mock.patch('aws_lambda_fsm.aws.settings')
    @mock.patch('aws_lambda_fsm.aws.boto3')
    def test_get_elasticache_engine_and_endpoint_on_wire(self,
                                                         mock_boto3,
                                                         mock_settings):
        setattr(_local, 'cache_details_for_' + _get_test_arn(AWS.ELASTICACHE), None)
        mock_boto3.client.return_value.describe_cache_clusters.return_value = {
            'CacheClusters': [{
                'Engine': 'memcached',
                'ConfigurationEndpoint': {
                    'Port': 11211,
                    'Address': 'foobar.cfg.cache.amazonaws.com'
                }
            }]
        }
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = {}
        actual = _get_elasticache_engine_and_endpoint(_get_test_arn(AWS.ELASTICACHE))
        expected = ('memcached', 'foobar.cfg.cache.amazonaws.com:11211')
        self.assertEqual(expected, actual)

    # _get_connection_info

    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_get_connection_info_looks_up_by_arn(self,
                                                 mock_settings):
        mock_settings.ENDPOINTS = {'testarn': 'test://test:111/test'}
        actual = _get_connection_info('testservice', 'testregion', 'testarn')
        expected = 'testservice', 'testing', 'test://test:111/test'
        self.assertEqual(expected, actual)

    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_get_connection_info_looks_up_by_service_and_region(self,
                                                                mock_settings):
        mock_settings.ENDPOINTS = {'testservice': {'testregion': 'test://test:111/test'}}
        actual = _get_connection_info('testservice', 'testregion', 'testarn')
        expected = 'testservice', 'testing', 'test://test:111/test'
        self.assertEqual(expected, actual)

    @mock.patch('aws_lambda_fsm.aws.settings')
    @mock.patch('aws_lambda_fsm.aws.os.environ')
    def test_get_connection_info_looks_up_by_environ(self,
                                                     mock_environ,
                                                     mock_settings):
        mock_settings.ENDPOINTS = {}
        mock_environ.get.return_value = 'test://test:111/test'
        actual = _get_connection_info('testservice', 'testregion', 'testarn')
        expected = 'testservice', 'testing', 'test://test:111/test'
        self.assertEqual(expected, actual)

    @mock.patch('aws_lambda_fsm.aws.settings')
    @mock.patch('aws_lambda_fsm.aws.os.environ')
    def test_get_connection_info_returns_original_if_no_endpoints(self,
                                                                  mock_environ,
                                                                  mock_settings):
        mock_settings.ENDPOINTS = {}
        mock_environ.get.return_value = None
        actual = _get_connection_info('testservice', 'testregion', 'testarn')
        expected = 'testservice', 'testregion', None
        self.assertEqual(expected, actual)

    # _get_service_connection

    @mock.patch('aws_lambda_fsm.aws._get_connection_info')
    def test_get_service_connection_sets_local_var(self,
                                                   mock_get_connection_info):
        mock_get_connection_info.return_value = 'kinesis', 'testing', 'http://localhost:1234'
        setattr(_local, 'connection_to_' + _get_test_arn(AWS.KINESIS), None)
        conn = _get_service_connection(_get_test_arn(AWS.KINESIS))
        self.assertIsNotNone(conn)
        self.assertIsNotNone(getattr(_local, 'connection_to_' + _get_test_arn(AWS.KINESIS)))

    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_get_service_connection_memcache_exists(self, mock_settings):
        mock_settings.ENDPOINTS = ENDPOINTS_MEMCACHE
        setattr(_local, 'connection_to_' + _get_test_arn(AWS.ELASTICACHE), None)
        conn = _get_service_connection(_get_test_arn(AWS.ELASTICACHE))
        self.assertIsNotNone(conn)
        self.assertIsNotNone(getattr(_local, 'connection_to_' + _get_test_arn(AWS.ELASTICACHE)))
        self.assertTrue(
            isinstance(
                getattr(_local, 'connection_to_' + _get_test_arn(AWS.ELASTICACHE)).wrapped_connection,
                memcache.Client
            )
        )

    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_get_service_connection_redis_exists(self, mock_settings):
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        setattr(_local, 'connection_to_' + _get_test_arn(AWS.ELASTICACHE), None)
        conn = _get_service_connection(_get_test_arn(AWS.ELASTICACHE))
        self.assertIsNotNone(conn)
        self.assertIsNotNone(getattr(_local, 'connection_to_' + _get_test_arn(AWS.ELASTICACHE)))
        self.assertTrue(
            isinstance(
                getattr(_local, 'connection_to_' + _get_test_arn(AWS.ELASTICACHE)).wrapped_connection,
                redis.StrictRedis
            )
        )

    @mock.patch('aws_lambda_fsm.aws._get_connection_info')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_get_service_connection_chaos(self,
                                          mock_settings,
                                          mock_get_connection_info):
        mock_get_connection_info.return_value = 'kinesis', 'testing', 'http://localhost:1234'
        mock_settings.CHAOS = {'foo': 'bar'}
        conn = _get_service_connection(_get_test_arn(AWS.DYNAMODB))
        self.assertTrue(isinstance(conn, ChaosConnection))

    @mock.patch('aws_lambda_fsm.aws._get_connection_info')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_get_service_connection_no_chaos(self,
                                             mock_settings,
                                             mock_get_connection_info):
        mock_get_connection_info.return_value = 'kinesis', 'testing', 'http://localhost:1234'
        mock_settings.CHAOS = {'foo': 'bar'}
        conn = _get_service_connection(_get_test_arn(AWS.DYNAMODB), disable_chaos=True)
        self.assertFalse(isinstance(conn, ChaosConnection))

    # get_connection

    @mock.patch('aws_lambda_fsm.aws._get_service_connection')
    def test_get_kinesis_connection(self,
                                    mock_get_service_connection):
        setattr(_local, 'connection_to_' + _get_test_arn(AWS.KINESIS), None)
        conn = get_connection(_get_test_arn(AWS.KINESIS))
        self.assertIsNotNone(conn)
        mock_get_service_connection.assert_called_with(_get_test_arn(AWS.KINESIS),
                                                       connect_timeout=60,
                                                       read_timeout=60,
                                                       disable_chaos=False)

    @mock.patch('aws_lambda_fsm.aws._get_service_connection')
    def test_get_memcache_connection(self,
                                     mock_get_service_connection):
        setattr(_local, 'connection_to_' + _get_test_arn(AWS.ELASTICACHE), None)
        conn = get_connection(_get_test_arn(AWS.ELASTICACHE))
        self.assertIsNotNone(conn)
        mock_get_service_connection.assert_called_with(_get_test_arn(AWS.ELASTICACHE),
                                                       connect_timeout=60,
                                                       read_timeout=60,
                                                       disable_chaos=False)

    @mock.patch('aws_lambda_fsm.aws._get_service_connection')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_get_redis_connection(self,
                                  mock_settings,
                                  mock_get_service_connection):
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        setattr(_local, 'connection_to_' + _get_test_arn(AWS.ELASTICACHE), None)
        conn = get_connection(_get_test_arn(AWS.ELASTICACHE))
        self.assertIsNotNone(conn)
        mock_get_service_connection.assert_called_with(_get_test_arn(AWS.ELASTICACHE),
                                                       connect_timeout=60,
                                                       read_timeout=60,
                                                       disable_chaos=False)

    # _get_sqs_queue_url

    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_get_sqs_queue_url_from_settings(self,
                                             mock_settings):
        setattr(_local, 'url_for_' + _get_test_arn(AWS.SQS), None)
        arn = _get_test_arn(AWS.SQS)
        mock_settings.SQS_URLS = {
            _get_test_arn(AWS.SQS): {
                AWS_SQS.QueueUrl: 'https://sqs.testing.amazonaws.com/1234567890/settings'
            }
        }
        expected = 'https://sqs.testing.amazonaws.com/1234567890/settings'
        actual = _get_sqs_queue_url(arn)
        self.assertEqual(expected, actual)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    def test_get_sqs_queue_url(self,
                               mock_get_connection):
        setattr(_local, 'url_for_' + _get_test_arn(AWS.SQS), None)
        arn = _get_test_arn(AWS.SQS)
        mock_get_connection.return_value.get_queue_url.return_value = {
            AWS_SQS.QueueUrl: 'https://sqs.testing.amazonaws.com/1234567890/queuename'
        }
        expected = 'https://sqs.testing.amazonaws.com/1234567890/queuename'
        actual = _get_sqs_queue_url(arn)
        self.assertEqual(expected, actual)
        self.assertEqual(expected, getattr(_local, 'url_for_' + arn))

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    def test_get_sqs_queue_url_missing(self,
                                       mock_get_connection):
        setattr(_local, 'url_for_' + _get_test_arn(AWS.SQS), None)
        arn = _get_test_arn(AWS.SQS)
        mock_get_connection.return_value.get_queue_url.return_value = {
            'foobar': 'https://sqs.testing.amazonaws.com/1234567890/queuename'
        }
        actual = _get_sqs_queue_url(arn)
        self.assertIsNone(actual)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    def test_get_sqs_queue_url_uses_local_cache(self,
                                                mock_get_connection):
        setattr(_local, 'url_for_' + _get_test_arn(AWS.SQS), None)
        arn = _get_test_arn(AWS.SQS)
        mock_get_connection.return_value.get_queue_url.return_value = {
            AWS_SQS.QueueUrl: 'https://sqs.testing.amazonaws.com/1234567890/queuename'
        }
        expected = 'https://sqs.testing.amazonaws.com/1234567890/queuename'
        _get_sqs_queue_url(arn)
        self.assertEqual(expected, getattr(_local, 'url_for_' + arn))
        mock_get_connection.return_value.get_queue_url.return_value = {
            AWS_SQS.QueueUrl: 'foobar'
        }
        _get_sqs_queue_url(arn)
        self.assertEqual(expected, getattr(_local, 'url_for_' + arn))
        delattr(_local, 'url_for_' + arn)
        _get_sqs_queue_url(arn)
        self.assertEqual('foobar', getattr(_local, 'url_for_' + arn))

    ##################################################
    # Functions
    ##################################################

    # increment_error_counters

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.datetime')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_increment_error_counter(self,
                                     mock_settings,
                                     mock_datetime,
                                     mock_get_connection):
        mock_settings.PRIMARY_METRICS_SOURCE = _get_test_arn(AWS.CLOUDWATCH)
        mock_datetime.datetime.utcnow.return_value = 'now'
        increment_error_counters({'a': 98, 'b': 99}, {'d': 'e'})
        mock_get_connection.return_value.put_metric_data.assert_called_with(
            Namespace='resourcetype/resourcename',
            MetricData=[
                {
                    'Timestamp': 'now',
                    'Dimensions': [
                        {'Name': 'd', 'Value': 'e'}
                    ],
                    'Value': 98,
                    'MetricName': 'a'
                },
                {
                    'Timestamp': 'now',
                    'Dimensions': [
                        {'Name': 'd', 'Value': 'e'}
                    ],
                    'Value': 99,
                    'MetricName': 'b'
                }
            ]
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    def test_increment_error_counter_no_connection(self,
                                                   mock_get_connection):
        mock_get_connection.return_value = None
        ret = increment_error_counters([('b', 99)], {'d': 'e'})
        self.assertIsNone(ret)

    # get_primary_stream_source
    # get_secondary_stream_source

    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_get_primary_stream_source(self,
                                       mock_settings):
        mock_settings.PRIMARY_STREAM_SOURCE = 'foo'
        self.assertEqual('foo', get_primary_stream_source())

    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_get_secondary_stream_source(self,
                                         mock_settings):
        mock_settings.SECONDARY_STREAM_SOURCE = 'bar'
        self.assertEqual('bar', get_secondary_stream_source())

    # get_primary_environment_source
    # get_secondary_environment_source

    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_get_primary_environment_source(self,
                                            mock_settings):
        mock_settings.PRIMARY_ENVIRONMENT_SOURCE = 'foo'
        self.assertEqual('foo', get_primary_environment_source())

    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_get_secondary_environment_source(self,
                                              mock_settings):
        mock_settings.SECONDARY_ENVIRONMENT_SOURCE = 'bar'
        self.assertEqual('bar', get_secondary_environment_source())

    # get_primary_checkpoint_source
    # get_secondary_checkpoint_source

    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_get_primary_checkpoint_source(self,
                                           mock_settings):
        mock_settings.PRIMARY_CHECKPOINT_SOURCE = 'foo'
        self.assertEqual('foo', get_primary_checkpoint_source())

    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_get_secondary_checkpoint_source(self,
                                             mock_settings):
        mock_settings.SECONDARY_CHECKPOINT_SOURCE = 'bar'
        self.assertEqual('bar', get_secondary_checkpoint_source())

    # store_environment

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_environment_source')
    @mock.patch('aws_lambda_fsm.aws.uuid')
    def test_store_environment_dynamodb(self,
                                        mock_uuid,
                                        mock_get_primary_environment_source,
                                        mock_get_connection):
        mock_uuid.uuid4.return_value.hex = 'guid'
        mock_context = mock.Mock()
        mock_get_primary_environment_source.return_value = _get_test_arn(AWS.DYNAMODB)
        store_environment(mock_context, {'a': 'b'})
        mock_get_connection.return_value.put_item.assert_called_with(
            Item={'environment': {'S': '{"a": "b"}'}, 'guid': {'S': 'guid'}},
            TableName='resourcename'
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_secondary_environment_source')
    @mock.patch('aws_lambda_fsm.aws.uuid')
    def test_store_environment_dynamodb_secondary(self,
                                                  mock_uuid,
                                                  get_secondary_environment_source,
                                                  mock_get_connection):
        mock_uuid.uuid4.return_value.hex = 'guid'
        mock_context = mock.Mock()
        get_secondary_environment_source.return_value = _get_test_arn(AWS.DYNAMODB)
        store_environment(mock_context, {'a': 'b'}, primary=False)
        mock_get_connection.return_value.put_item.assert_called_with(
            Item={'environment': {'S': '{"a": "b"}'}, 'guid': {'S': 'guid'}},
            TableName='resourcename'
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_environment_source')
    @mock.patch('aws_lambda_fsm.aws.uuid')
    def test_store_environment_dynamodb_disabled(self,
                                                 mock_uuid,
                                                 mock_get_primary_environment_source,
                                                 mock_get_connection):
        mock_uuid.uuid4.return_value.hex = 'guid'
        mock_context = mock.Mock()
        mock_get_primary_environment_source.return_value = _get_test_arn(AWS.DYNAMODB)
        mock_get_connection.return_value = None
        store_environment(mock_context, {'a': 'b'})

    # load_environment

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_environment_source')
    def test_load_environment_dynamodb(self,
                                       mock_get_primary_environment_source,
                                       mock_get_connection):
        mock_context = mock.Mock()
        mock_get_primary_environment_source.return_value = _get_test_arn(AWS.DYNAMODB)
        mock_get_connection.return_value.get_item.return_value = \
            {'Item': {'environment': {'S': '{"a": "b"}'}, 'guid': {'S': 'guid'}}}
        env = load_environment(mock_context, _get_test_arn(AWS.DYNAMODB) + ';' + 'guid')
        self.assertEqual({'a': 'b'}, env)
        mock_get_connection.return_value.get_item.assert_called_with(
            ConsistentRead=True, TableName='resourcename', Key={'guid': {'S': 'guid'}}
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_secondary_environment_source')
    def test_load_environment_dynamodb_secondary(self,
                                                 mock_get_secondary_environment_source,
                                                 mock_get_connection):
        mock_context = mock.Mock()
        mock_get_secondary_environment_source.return_value = _get_test_arn(AWS.DYNAMODB)
        mock_get_connection.return_value.get_item.return_value = \
            {'Item': {'environment': {'S': '{"a": "b"}'}, 'guid': {'S': 'guid'}}}
        env = load_environment(mock_context, _get_test_arn(AWS.DYNAMODB) + ';' + 'guid', primary=False)
        self.assertEqual({'a': 'b'}, env)
        mock_get_connection.return_value.get_item.assert_called_with(
            ConsistentRead=True, TableName='resourcename', Key={'guid': {'S': 'guid'}}
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_environment_source')
    def test_load_environment_dynamodb_disabled(self,
                                                mock_get_primary_environment_source,
                                                mock_get_connection):
        mock_context = mock.Mock()
        mock_get_primary_environment_source.return_value = _get_test_arn(AWS.DYNAMODB)
        mock_get_connection.return_value = None
        load_environment(mock_context, _get_test_arn(AWS.DYNAMODB) + ';' + 'guid')

    # send_next_event_for_dispatch

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_stream_source')
    def test_send_next_event_for_dispatch_kinesis(self,
                                                  mock_get_primary_stream_source,
                                                  mock_get_connection):
        mock_context = mock.Mock()
        mock_get_primary_stream_source.return_value = _get_test_arn(AWS.KINESIS)
        send_next_event_for_dispatch(mock_context, 'c', 'd')
        mock_get_connection.return_value.put_record.assert_called_with(
            PartitionKey='d',
            Data='c',
            StreamName='resourcename'
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_retry_source')
    def test_send_next_event_for_dispatch_kinesis_recovering(self,
                                                             mock_get_primary_retry_source,
                                                             mock_get_connection):
        mock_context = mock.Mock()
        mock_get_primary_retry_source.return_value = _get_test_arn(AWS.KINESIS)
        send_next_event_for_dispatch(mock_context, 'c', 'd', recovering=True)
        mock_get_connection.return_value.put_record.assert_called_with(
            PartitionKey='d',
            Data='c',
            StreamName='resourcename'
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_secondary_stream_source')
    def test_send_next_event_for_dispatch_kinesis_secondary(self,
                                                            mock_get_secondary_stream_source,
                                                            mock_get_connection):
        mock_context = mock.Mock()
        mock_get_secondary_stream_source.return_value = _get_test_arn(AWS.KINESIS)
        send_next_event_for_dispatch(mock_context, 'c', 'd', primary=False)
        mock_get_connection.return_value.put_record.assert_called_with(
            PartitionKey='d',
            Data='c',
            StreamName='resourcename'
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_secondary_retry_source')
    def test_send_next_event_for_dispatch_kinesis_secondary_recovering(self,
                                                                       mock_get_secondary_retry_source,
                                                                       mock_get_connection):
        mock_context = mock.Mock()
        mock_get_secondary_retry_source.return_value = _get_test_arn(AWS.KINESIS)
        send_next_event_for_dispatch(mock_context, 'c', 'd', primary=False, recovering=True)
        mock_get_connection.return_value.put_record.assert_called_with(
            PartitionKey='d',
            Data='c',
            StreamName='resourcename'
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_stream_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    def test_send_next_event_for_dispatch_dynamodb(self,
                                                   mock_time,
                                                   mock_get_primary_stream_source,
                                                   mock_get_connection):
        mock_context = mock.Mock()
        mock_time.time.return_value = 1234.0
        mock_get_primary_stream_source.return_value = _get_test_arn(AWS.DYNAMODB)
        send_next_event_for_dispatch(mock_context, 'c', 'd')
        mock_get_connection.return_value.put_item.assert_called_with(
            Item={'timestamp': {'N': '1234'}, 'correlation_id': {'S': 'd'}, 'payload': {'S': 'c'}},
            TableName='resourcename'
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_stream_source')
    def test_send_next_event_for_dispatch_sns(self,
                                              mock_get_primary_stream_source,
                                              mock_get_connection):
        mock_context = mock.Mock()
        mock_get_primary_stream_source.return_value = _get_test_arn(AWS.SNS)
        send_next_event_for_dispatch(mock_context, 'c', 'd')
        mock_get_connection.return_value.publish.assert_called_with(
            Message='{"default": "c"}',
            TopicArn=_get_test_arn(AWS.SNS)
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_stream_source')
    @mock.patch('aws_lambda_fsm.aws._get_sqs_queue_url')
    def test_send_next_event_for_dispatch_sqs(self,
                                              mock_get_sqs_queue_url,
                                              mock_get_primary_stream_source,
                                              mock_get_connection):
        mock_context = mock.Mock()
        mock_get_primary_stream_source.return_value = _get_test_arn(AWS.SQS)
        mock_get_sqs_queue_url.return_value = 'https://sqs.testing.amazonaws.com/1234567890/queuename'
        send_next_event_for_dispatch(mock_context, 'c', 'd')
        mock_get_connection.return_value.send_message.assert_called_with(
            QueueUrl='https://sqs.testing.amazonaws.com/1234567890/queuename',
            DelaySeconds=0,
            MessageBody='c'
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_stream_source')
    def test_send_next_events_for_dispatch_kinesis(self,
                                                   mock_get_primary_stream_source,
                                                   mock_get_connection):
        mock_context = mock.Mock()
        mock_get_primary_stream_source.return_value = _get_test_arn(AWS.KINESIS)
        send_next_events_for_dispatch(mock_context, ['c', 'cc'], ['d', 'dd'])
        mock_get_connection.return_value.put_records.assert_called_with(
            Records=[{'PartitionKey': 'd', 'Data': 'c'}, {'PartitionKey': 'dd', 'Data': 'cc'}],
            StreamName='resourcename'
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_secondary_stream_source')
    def test_send_next_events_for_dispatch_kinesis_secondary(self,
                                                             mock_get_secondary_stream_source,
                                                             mock_get_connection):
        mock_context = mock.Mock()
        mock_get_secondary_stream_source.return_value = _get_test_arn(AWS.KINESIS)
        send_next_events_for_dispatch(mock_context, ['c', 'cc'], ['d', 'dd'], primary=False)
        mock_get_connection.return_value.put_records.assert_called_with(
            Records=[{'PartitionKey': 'd', 'Data': 'c'}, {'PartitionKey': 'dd', 'Data': 'cc'}],
            StreamName='resourcename'
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_stream_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    def test_send_next_events_for_dispatch_dynamodb(self,
                                                    mock_time,
                                                    mock_get_primary_stream_source,
                                                    mock_get_connection):
        mock_context = mock.Mock()
        mock_time.time.return_value = 1234.0
        mock_get_primary_stream_source.return_value = _get_test_arn(AWS.DYNAMODB)
        send_next_events_for_dispatch(mock_context, ['c', 'cc'], ['d', 'dd'])
        mock_get_connection.return_value.batch_write_item.assert_called_with(
            RequestItems={
                'resourcename': [
                    {'PutRequest': {'Item': {'timestamp': {'N': '1234'},
                                             'correlation_id': {'S': 'd'}, 'payload': {'S': 'c'}}}},
                    {'PutRequest': {'Item': {'timestamp': {'N': '1234'},
                                             'correlation_id': {'S': 'dd'}, 'payload': {'S': 'cc'}}}}
                ]
            }
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_stream_source')
    def test_send_next_events_for_dispatch_sns(self,
                                               mock_get_primary_stream_source,
                                               mock_get_connection):
        mock_context = mock.Mock()
        mock_get_primary_stream_source.return_value = _get_test_arn(AWS.SNS)
        send_next_events_for_dispatch(mock_context, ['c', 'cc'], ['d', 'dd'])
        mock_get_connection.return_value.publish.assert_has_calls(
            [
                mock.call(Message='{"default": "c"}', TopicArn=_get_test_arn(AWS.SNS)),
                mock.call(Message='{"default": "cc"}', TopicArn=_get_test_arn(AWS.SNS))
            ], any_order=True
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_stream_source')
    @mock.patch('aws_lambda_fsm.aws._get_sqs_queue_url')
    def test_send_next_events_for_dispatch_sqs(self,
                                               mock_get_sqs_queue_url,
                                               mock_get_primary_stream_source,
                                               mock_get_connection):
        mock_context = mock.Mock()
        mock_get_primary_stream_source.return_value = _get_test_arn(AWS.SQS)
        mock_get_sqs_queue_url.return_value = 'https://sqs.testing.amazonaws.com/1234567890/queuename'
        send_next_events_for_dispatch(mock_context, ['c', 'cc'], ['d', 'dd'])
        mock_get_connection.return_value.send_message_batch.assert_called_with(
            QueueUrl='https://sqs.testing.amazonaws.com/1234567890/queuename',
            Entries=[{'DelaySeconds': 0, 'Id': 'd', 'MessageBody': 'c'},
                     {'DelaySeconds': 0, 'Id': 'dd', 'MessageBody': 'cc'}]
        )

    # retriable_entities

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    def test_retriable_entities(self,
                                mock_get_connection):
        mock_get_connection.return_value.query.return_value = {
            'Items': [{'payload': {'S': 'a'}, 'correlation_id_steps': {'S': 'b'}}]
        }
        items = retriable_entities(_get_test_arn(AWS.DYNAMODB), 'b', 'c')
        self.assertTrue(items)
        mock_get_connection.return_value.query.assert_called_with(
            TableName='resourcename',
            ConsistentRead=True,
            Limit=100,
            IndexName='b',
            KeyConditions={'partition': {'ComparisonOperator': 'EQ',
                                         'AttributeValueList': [{'N': '15'}]},
                           'run_at': {'ComparisonOperator': 'LT',
                                      'AttributeValueList': [{'N': 'c'}]}}
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    def test_retriable_entities_no_connection(self,
                                              mock_get_connection):
        mock_get_connection.return_value = None
        iter = retriable_entities('a', 'b', 'c')
        self.assertEqual([], iter)

    # start_retries

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_start_retries_primary(self,
                                   mock_settings,
                                   mock_get_connection):
        mock_context = mock.Mock()
        mock_context.correlation_id = 'b'
        mock_context.steps = 'z'
        mock_settings.PRIMARY_RETRY_SOURCE = _get_test_arn(AWS.DYNAMODB)
        start_retries(mock_context, 'c', 'd', primary=True)
        mock_get_connection.return_value.put_item.assert_called_with(
            Item={'partition': {'N': '15'},
                  'payload': {'S': 'd'},
                  'correlation_id_steps': {'S': 'b-z'},
                  'run_at': {'N': 'c'}},
            TableName='resourcename'
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_start_retries_primary_recovering(self,
                                              mock_settings,
                                              mock_get_connection):
        mock_context = mock.Mock()
        mock_context.correlation_id = 'b'
        mock_context.steps = 'z'
        mock_settings.PRIMARY_STREAM_SOURCE = _get_test_arn(AWS.DYNAMODB)
        start_retries(mock_context, 'c', 'd', primary=True, recovering=True)
        mock_get_connection.return_value.put_item.assert_called_with(
            Item={'partition': {'N': '15'},
                  'payload': {'S': 'd'},
                  'correlation_id_steps': {'S': 'b-z'},
                  'run_at': {'N': 'c'}},
            TableName='resourcename'
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_start_retries_secondary(self,
                                     mock_settings,
                                     mock_get_connection):
        mock_context = mock.Mock()
        mock_context.correlation_id = 'b'
        mock_settings.SECONDARY_RETRY_SOURCE = _get_test_arn(AWS.KINESIS)
        start_retries(mock_context, 'c', 'd', primary=False)
        mock_get_connection.return_value.put_record.assert_called_with(
            PartitionKey='b',
            Data='d',
            StreamName='resourcename'
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_start_retries_secondary_recovering(self,
                                                mock_settings,
                                                mock_get_connection):
        mock_context = mock.Mock()
        mock_context.correlation_id = 'b'
        mock_settings.SECONDARY_STREAM_SOURCE = _get_test_arn(AWS.KINESIS)
        start_retries(mock_context, 'c', 'd', primary=False, recovering=True)
        mock_get_connection.return_value.put_record.assert_called_with(
            PartitionKey='b',
            Data='d',
            StreamName='resourcename'
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_start_retries_sns(self,
                               mock_settings,
                               mock_get_connection):
        mock_context = mock.Mock()
        mock_context.correlation_id = 'b'
        mock_settings.PRIMARY_RETRY_SOURCE = _get_test_arn(AWS.SNS)
        start_retries(mock_context, 'c', 'd', primary=True)
        mock_get_connection.return_value.publish.assert_called_with(
            Message='{"default": "d"}',
            TopicArn=_get_test_arn(AWS.SNS)
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.settings')
    @mock.patch('aws_lambda_fsm.aws._get_sqs_queue_url')
    def test_start_retries_sqs(self,
                               mock_get_sqs_queue_url,
                               mock_settings,
                               mock_get_connection):
        mock_context = mock.Mock()
        mock_context.correlation_id = 'b'
        mock_settings.PRIMARY_RETRY_SOURCE = _get_test_arn(AWS.SQS)
        mock_get_sqs_queue_url.return_value = 'https://sqs.testing.amazonaws.com/1234567890/queuename'
        start_retries(mock_context, 123, 'd', primary=True)
        mock_get_connection.return_value.send_message.assert_called_with(
            QueueUrl='https://sqs.testing.amazonaws.com/1234567890/queuename',
            DelaySeconds=0,
            MessageBody='d'
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    def test_start_retries_no_connection(self,
                                         mock_get_connection):
        mock_context = mock.Mock()
        mock_get_connection.return_value = None
        ret = start_retries(mock_context, 'c', 'd')
        self.assertIsNone(ret)

    # stop_retries

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_stop_retries_primary(self,
                                  mock_settings,
                                  mock_get_connection):
        mock_context = mock.Mock()
        mock_context.correlation_id = 'b'
        mock_context.steps = 'z'
        mock_settings.PRIMARY_RETRY_SOURCE = _get_test_arn(AWS.DYNAMODB)
        stop_retries(mock_context, primary=True)
        mock_get_connection.return_value.delete_item.assert_called_with(
            Key={'partition': {'N': '15'},
                 'correlation_id_steps': {'S': 'b-z'}},
            TableName='resourcename'
        )

    def test_stop_retries_secondary(self):
        mock_context = mock.Mock()
        mock_context.correlation_id = 'b'
        stop_retries(mock_context, primary=False)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    def test_stop_retries_no_connection(self,
                                        mock_get_connection):
        mock_context = mock.Mock()
        mock_get_connection.return_value = None
        ret = stop_retries(mock_context)
        self.assertIsNone(ret)

    # store_checkpoint

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_checkpoint_source')
    def test_store_checkpoint_kinesis(self,
                                      mock_get_primary_checkpoint_source,
                                      mock_get_connection):
        mock_context = mock.Mock()
        mock_get_primary_checkpoint_source.return_value = _get_test_arn(AWS.KINESIS)
        store_checkpoint(mock_context, 'd')
        self.assertFalse(mock_get_connection.return_value.put_record.called)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_secondary_checkpoint_source')
    def test_store_checkpoint_dynamodb(self,
                                       mock_get_secondary_stream_source,
                                       mock_get_connection):
        mock_context = mock.Mock()
        mock_context.correlation_id = 'c'
        mock_get_secondary_stream_source.return_value = _get_test_arn(AWS.DYNAMODB)
        store_checkpoint(mock_context, 'd', primary=False)
        mock_get_connection.return_value.put_item.assert_called_with(
            Item={'sent': {'S': 'd'},
                  'correlation_id': {'S': 'c'}},
            TableName='resourcename'
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    def test_store_checkpoint_no_connection(self,
                                            mock_get_connection):
        mock_context = mock.Mock()
        mock_get_connection.return_value = None
        ret = store_checkpoint(mock_context, 'c', 'd')
        self.assertIsNone(ret)

    # set_message_dispatched

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    def test_set_message_dispatched_no_connection(self,
                                                  mock_get_connection):
        mock_get_connection.return_value = None
        ret = set_message_dispatched('a', 'b', 'c')
        self.assertIsNone(ret)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    def test_set_message_dispatched_memcache(self,
                                             mock_get_primary_cache_source,
                                             mock_get_connection):
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        ret = set_message_dispatched('a', 'b', 'c')
        self.assertTrue(ret)
        mock_get_connection.return_value.set.assert_called_with('a-b', 'a-b-c')

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    def test_set_message_dispatched_memcache_connection_error(self,
                                                              mock_get_primary_cache_source,
                                                              mock_get_connection):
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.set.return_value = 0
        ret = set_message_dispatched('a', 'b', 'c')
        self.assertTrue(0 is ret)
        mock_get_connection.return_value.set.assert_called_with('a-b', 'a-b-c')

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_set_message_dispatched_redis(self,
                                          mock_settings,
                                          mock_get_primary_cache_source,
                                          mock_get_connection):
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        ret = set_message_dispatched('a', 'b', 'c')
        self.assertTrue(ret)
        mock_get_connection.return_value.set.assert_called_with('a-b', 'a-b-c')

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_set_message_dispatched_redis_connection_error(self,
                                                           mock_settings,
                                                           mock_get_primary_cache_source,
                                                           mock_get_connection):
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.set.side_effect = redis.exceptions.ConnectionError
        ret = set_message_dispatched('a', 'b', 'c')
        self.assertTrue(0 is ret)
        mock_get_connection.return_value.set.assert_called_with('a-b', 'a-b-c')

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    def test_set_message_dispatched_dynamodb(self,
                                             mock_get_primary_cache_source,
                                             mock_get_connection):
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.DYNAMODB)
        ret = set_message_dispatched('a', 'b', 'c')
        self.assertTrue(ret)
        mock_get_connection.return_value.put_item.assert_called_with(
            Item={'ckey': {'S': 'a-b'}, 'value': {'S': 'a-b-c'}},
            TableName='resourcename'
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_secondary_cache_source')
    def test_set_message_dispatched_dynamodb_secondary(self,
                                                       mock_get_secondary_cache_source,
                                                       mock_get_connection):
        mock_get_secondary_cache_source.return_value = _get_test_arn(AWS.DYNAMODB)
        ret = set_message_dispatched('a', 'b', 'c', primary=False)
        self.assertTrue(ret)
        mock_get_connection.return_value.put_item.assert_called_with(
            Item={'ckey': {'S': 'a-b'}, 'value': {'S': 'a-b-c'}},
            TableName='resourcename'
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    def test_set_message_dispatched_dynamodb_error(self,
                                                   mock_get_primary_cache_source,
                                                   mock_get_connection):
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.DYNAMODB)
        mock_get_connection.return_value.put_item.side_effect = \
            ClientError({'Error': {'Code': 'ConditionalCheckFailedException'}}, 'Operation')
        ret = set_message_dispatched('a', 'b', 'c')
        self.assertTrue(0 is ret)
        mock_get_connection.return_value.put_item.assert_called_with(
            Item={'ckey': {'S': 'a-b'}, 'value': {'S': 'a-b-c'}},
            TableName='resourcename'
        )

    # get_message_dispatched

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    def test_get_message_dispatched_no_connection(self,
                                                  mock_get_connection):
        mock_get_connection.return_value = None
        ret = get_message_dispatched('a', 'b')
        self.assertFalse(ret)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    def test_get_message_dispatched_memcache(self,
                                             mock_get_primary_cache_source,
                                             mock_get_connection):
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.get.return_value = 'foobar'
        ret = get_message_dispatched('a', 'b')
        self.assertEqual('foobar', ret)
        mock_get_connection.return_value.get.assert_called_with('a-b')

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    def test_get_message_dispatched_memcache_connection_error(self,
                                                              mock_get_primary_cache_source,
                                                              mock_get_connection):
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.get.return_value = None
        ret = get_message_dispatched('a', 'b')
        self.assertIsNone(ret)
        mock_get_connection.return_value.get.assert_called_with('a-b')

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_get_message_dispatched_redis(self,
                                          mock_settings,
                                          mock_get_primary_cache_source,
                                          mock_get_connection):
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.get.return_value = 'foobar'
        ret = get_message_dispatched('a', 'b')
        self.assertEqual('foobar', ret)
        mock_get_connection.return_value.get.assert_called_with('a-b')

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_get_message_dispatched_redis_connection_error(self,
                                                           mock_settings,
                                                           mock_get_primary_cache_source,
                                                           mock_get_connection):
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.get.side_effect = redis.exceptions.ConnectionError
        ret = get_message_dispatched('a', 'b')
        self.assertIsNone(ret)
        mock_get_connection.return_value.get.assert_called_with('a-b')

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    def test_get_message_dispatched_dynamodb(self,
                                             mock_get_primary_cache_source,
                                             mock_get_connection):
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.DYNAMODB)
        mock_get_connection.return_value.get_item.return_value = {'Item': {'value': {'S': 'foobar'}}}
        ret = get_message_dispatched('a', 'b')
        self.assertEqual('foobar', ret)
        mock_get_connection.return_value.get_item.assert_called_with(
            ConsistentRead=True,
            TableName='resourcename',
            Key={'ckey': {'S': 'a-b'}}
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_secondary_cache_source')
    def test_get_message_dispatched_dynamodb_secondary(self,
                                                       mock_get_secondary_cache_source,
                                                       mock_get_connection):
        mock_get_secondary_cache_source.return_value = _get_test_arn(AWS.DYNAMODB)
        mock_get_connection.return_value.get_item.return_value = {'Item': {'value': {'S': 'foobar'}}}
        ret = get_message_dispatched('a', 'b', primary=False)
        self.assertEqual('foobar', ret)
        mock_get_connection.return_value.get_item.assert_called_with(
            ConsistentRead=True,
            TableName='resourcename',
            Key={'ckey': {'S': 'a-b'}}
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    def test_get_message_dispatched_dynamodb_error(self,
                                                   mock_get_primary_cache_source,
                                                   mock_get_connection):
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.DYNAMODB)
        mock_get_connection.return_value.get_item.side_effect = \
            ClientError({'Error': {'Code': 'ConditionalCheckFailedException'}}, 'Operation')
        ret = get_message_dispatched('a', 'b')
        self.assertIsNone(ret)
        mock_get_connection.return_value.get_item.assert_called_with(
            ConsistentRead=True,
            TableName='resourcename',
            Key={'ckey': {'S': 'a-b'}}
        )


class LeaseMemcacheTest(unittest.TestCase):

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_secondary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_aquire_lease_memcache_failure(self,
                                           mock_settings,
                                           mock_time,
                                           mock_get_secondary_cache_source,
                                           mock_get_connection):
        mock_settings.ENDPOINTS = ENDPOINTS_MEMCACHE
        mock_time.time.return_value = 999.
        mock_get_secondary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.gets.return_value = None
        mock_get_connection.return_value.cas.return_value = 0
        ret = acquire_lease('a', 1, 1, primary=False)
        self.assertTrue(0 is ret)
        mock_get_connection.return_value.gets.assert_called_with('lease-a')
        mock_get_connection.return_value.cas.assert_called_with('lease-a', '1-1-1299', time=300)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_secondary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_aquire_lease_memcache_available_lose_secondary(self,
                                                            mock_settings,
                                                            mock_time,
                                                            mock_get_secondary_cache_source,
                                                            mock_get_connection):
        mock_settings.ENDPOINTS = ENDPOINTS_MEMCACHE
        mock_time.time.return_value = 999.
        mock_get_secondary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.gets.return_value = None
        mock_get_connection.return_value.cas.return_value = False
        ret = acquire_lease('a', 1, 1, primary=False)
        self.assertFalse(ret)
        mock_get_connection.return_value.gets.assert_called_with('lease-a')
        mock_get_connection.return_value.cas.assert_called_with('lease-a', '1-1-1299', time=300)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_aquire_lease_memcache_available_lose(self,
                                                  mock_settings,
                                                  mock_time,
                                                  mock_get_primary_cache_source,
                                                  mock_get_connection):
        mock_settings.ENDPOINTS = ENDPOINTS_MEMCACHE
        mock_time.time.return_value = 999.
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.gets.return_value = None
        mock_get_connection.return_value.cas.return_value = False
        ret = acquire_lease('a', 1, 1)
        self.assertFalse(ret)
        mock_get_connection.return_value.gets.assert_called_with('lease-a')
        mock_get_connection.return_value.cas.assert_called_with('lease-a', '1-1-1299', time=300)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_aquire_lease_memcache_available_wins(self,
                                                  mock_settings,
                                                  mock_time,
                                                  mock_get_primary_cache_source,
                                                  mock_get_connection):
        mock_settings.ENDPOINTS = ENDPOINTS_MEMCACHE
        mock_time.time.return_value = 999.
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.gets.return_value = None
        mock_get_connection.return_value.cas.return_value = True
        ret = acquire_lease('a', 1, 1)
        self.assertTrue(ret)
        mock_get_connection.return_value.gets.assert_called_with('lease-a')
        mock_get_connection.return_value.cas.assert_called_with('lease-a', '1-1-1299', time=300)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_aquire_lease_memcache_leased_expired_lose(self,
                                                       mock_settings,
                                                       mock_time,
                                                       mock_get_primary_cache_source,
                                                       mock_get_connection):
        mock_settings.ENDPOINTS = ENDPOINTS_MEMCACHE
        mock_time.time.return_value = 999.
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.gets.return_value = '99-99-0'
        mock_get_connection.return_value.cas.return_value = False
        ret = acquire_lease('a', 1, 1)
        self.assertFalse(ret)
        mock_get_connection.return_value.gets.assert_called_with('lease-a')
        mock_get_connection.return_value.cas.assert_called_with('lease-a', '1-1-1299', time=300)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_aquire_lease_memcache_leased_expired_wins(self,
                                                       mock_settings,
                                                       mock_time,
                                                       mock_get_primary_cache_source,
                                                       mock_get_connection):
        mock_settings.ENDPOINTS = ENDPOINTS_MEMCACHE
        mock_time.time.return_value = 999.
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.gets.return_value = '99-99-0'
        mock_get_connection.return_value.cas.return_value = True
        ret = acquire_lease('a', 1, 1)
        self.assertTrue(ret)
        mock_get_connection.return_value.gets.assert_called_with('lease-a')
        mock_get_connection.return_value.cas.assert_called_with('lease-a', '1-1-1299', time=300)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_aquire_lease_memcache_leased_owned_lose(self,
                                                     mock_settings,
                                                     mock_time,
                                                     mock_get_primary_cache_source,
                                                     mock_get_connection):
        mock_settings.ENDPOINTS = ENDPOINTS_MEMCACHE
        mock_time.time.return_value = 999.
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.gets.return_value = '99-99-99'
        mock_get_connection.return_value.cas.return_value = False
        ret = acquire_lease('a', 99, 99)
        self.assertFalse(ret)
        mock_get_connection.return_value.gets.assert_called_with('lease-a')
        mock_get_connection.return_value.cas.assert_called_with('lease-a', '99-99-1299', time=300)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_aquire_lease_memcache_leased_owned_wins(self,
                                                     mock_settings,
                                                     mock_time,
                                                     mock_get_primary_cache_source,
                                                     mock_get_connection):
        mock_settings.ENDPOINTS = ENDPOINTS_MEMCACHE
        mock_time.time.return_value = 999.
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.gets.return_value = '99-99-99'
        mock_get_connection.return_value.cas.return_value = True
        ret = acquire_lease('a', 99, 99)
        self.assertTrue(ret)
        mock_get_connection.return_value.gets.assert_called_with('lease-a')
        mock_get_connection.return_value.cas.assert_called_with('lease-a', '99-99-1299', time=300)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_aquire_lease_memcache_leased_fall_through(self,
                                                       mock_settings,
                                                       mock_time,
                                                       mock_get_primary_cache_source,
                                                       mock_get_connection):
        mock_settings.ENDPOINTS = ENDPOINTS_MEMCACHE
        mock_time.time.return_value = 999.
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.gets.return_value = '99-99-999999999'
        ret = acquire_lease('a', 1, 1)
        self.assertFalse(ret)
        mock_get_connection.return_value.gets.assert_called_with('lease-a')
        self.assertFalse(mock_get_connection.return_value.cas.called)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_secondary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_release_lease_memcache_failure(self,
                                            mock_settings,
                                            mock_get_secondary_cache_source,
                                            mock_get_connection):
        mock_settings.ENDPOINTS = ENDPOINTS_MEMCACHE
        mock_get_secondary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.gets.return_value = "99-99-99"
        mock_get_connection.return_value.cas.return_value = 0
        ret = release_lease('a', 99, 99, 'f', primary=False)
        self.assertTrue(0 is ret)
        mock_get_connection.return_value.gets.assert_called_with('lease-a')
        mock_get_connection.return_value.cas.assert_called_with('lease-a', None, time=1)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_secondary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_release_lease_memcache_not_owned_secondary(self,
                                                        mock_settings,
                                                        mock_get_secondary_cache_source,
                                                        mock_get_connection):
        mock_settings.ENDPOINTS = ENDPOINTS_MEMCACHE
        mock_get_secondary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.gets.return_value = None
        mock_get_connection.return_value.cas.return_value = False
        ret = release_lease('a', 1, 1, 'f', primary=False)
        self.assertFalse(ret)
        mock_get_connection.return_value.gets.assert_called_with('lease-a')
        self.assertFalse(mock_get_connection.return_value.cas.called)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_release_lease_memcache_not_owned(self,
                                              mock_settings,
                                              mock_get_primary_cache_source,
                                              mock_get_connection):
        mock_settings.ENDPOINTS = ENDPOINTS_MEMCACHE
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.gets.return_value = None
        mock_get_connection.return_value.cas.return_value = False
        ret = release_lease('a', 1, 1, 'f')
        self.assertFalse(ret)
        mock_get_connection.return_value.gets.assert_called_with('lease-a')
        self.assertFalse(mock_get_connection.return_value.cas.called)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_release_lease_memcache_owned_self_loses(self,
                                                     mock_settings,
                                                     mock_get_primary_cache_source,
                                                     mock_get_connection):
        mock_settings.ENDPOINTS = ENDPOINTS_MEMCACHE
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.gets.return_value = '99-99-99'
        mock_get_connection.return_value.cas.return_value = False
        ret = release_lease('a', 99, 99, 'f')
        self.assertFalse(ret)
        mock_get_connection.return_value.gets.assert_called_with('lease-a')
        mock_get_connection.return_value.cas.assert_called_with('lease-a', None, time=1)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_release_lease_memcache_owned_self_wins(self,
                                                    mock_settings,
                                                    mock_get_primary_cache_source,
                                                    mock_get_connection):
        mock_settings.ENDPOINTS = ENDPOINTS_MEMCACHE
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.gets.return_value = '99-99-99'
        mock_get_connection.return_value.cas.return_value = True
        ret = release_lease('a', 99, 99, 'f')
        self.assertTrue(ret)
        mock_get_connection.return_value.gets.assert_called_with('lease-a')
        mock_get_connection.return_value.cas.assert_called_with('lease-a', None, time=1)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_release_lease_memcache_owned_other(self,
                                                mock_settings,
                                                mock_get_primary_cache_source,
                                                mock_get_connection):
        mock_settings.ENDPOINTS = ENDPOINTS_MEMCACHE
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_get_connection.return_value.gets.return_value = '99-99-99'
        mock_get_connection.return_value.cas.return_value = True
        ret = release_lease('a', 1, 1, 'f')
        self.assertFalse(ret)
        mock_get_connection.return_value.gets.assert_called_with('lease-a')
        self.assertFalse(mock_get_connection.return_value.cas.called)


class LeaseRedisTest(unittest.TestCase):

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_secondary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_aquire_lease_redis_failure(self,
                                        mock_settings,
                                        mock_time,
                                        mock_get_secondary_cache_source,
                                        mock_get_connection):
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        mock_time.time.return_value = 999.
        mock_get_secondary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_pipe = mock_get_connection.return_value.pipeline.return_value.__enter__.return_value
        mock_pipe.get.return_value = None
        mock_pipe.execute.side_effect = redis.exceptions.ConnectionError
        ret = acquire_lease('a', 1, 1, primary=False)
        self.assertTrue(0 is ret)
        mock_pipe.watch.assert_called_with('lease-a')
        mock_pipe.get.assert_called_with('lease-a')
        mock_pipe.multi.assert_called_with()
        mock_pipe.expire.assert_called_with('lease-a', 300)
        mock_pipe.set.assert_called_with('lease-a', '1-1-1299')
        mock_pipe.execute.assert_called_with()

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_secondary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_aquire_lease_redis_available_lose_secondary(self,
                                                         mock_settings,
                                                         mock_time,
                                                         mock_get_secondary_cache_source,
                                                         mock_get_connection):
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        mock_time.time.return_value = 999.
        mock_get_secondary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_pipe = mock_get_connection.return_value.pipeline.return_value.__enter__.return_value
        mock_pipe.get.return_value = None
        mock_pipe.execute.side_effect = redis.WatchError
        ret = acquire_lease('a', 1, 1, primary=False)
        self.assertFalse(ret)
        mock_pipe.watch.assert_called_with('lease-a')
        mock_pipe.get.assert_called_with('lease-a')
        mock_pipe.multi.assert_called_with()
        mock_pipe.expire.assert_called_with('lease-a', 300)
        mock_pipe.set.assert_called_with('lease-a', '1-1-1299')
        mock_pipe.execute.assert_called_with()

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_aquire_lease_redis_available_lose(self,
                                               mock_settings,
                                               mock_time,
                                               mock_get_primary_cache_source,
                                               mock_get_connection):
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        mock_time.time.return_value = 999.
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_pipe = mock_get_connection.return_value.pipeline.return_value.__enter__.return_value
        mock_pipe.get.return_value = None
        mock_pipe.execute.side_effect = redis.WatchError
        ret = acquire_lease('a', 1, 1)
        self.assertFalse(ret)
        mock_pipe.watch.assert_called_with('lease-a')
        mock_pipe.get.assert_called_with('lease-a')
        mock_pipe.multi.assert_called_with()
        mock_pipe.expire.assert_called_with('lease-a', 300)
        mock_pipe.set.assert_called_with('lease-a', '1-1-1299')
        mock_pipe.execute.assert_called_with()

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_aquire_lease_redis_available_wins(self,
                                               mock_settings,
                                               mock_time,
                                               mock_get_primary_cache_source,
                                               mock_get_connection):
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        mock_time.time.return_value = 999.
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_pipe = mock_get_connection.return_value.pipeline.return_value.__enter__.return_value
        mock_pipe.get.return_value = None
        ret = acquire_lease('a', 1, 1)
        self.assertTrue(ret)
        mock_pipe.watch.assert_called_with('lease-a')
        mock_pipe.get.assert_called_with('lease-a')
        mock_pipe.multi.assert_called_with()
        mock_pipe.expire.assert_called_with('lease-a', 300)
        mock_pipe.set.assert_called_with('lease-a', '1-1-1299')
        mock_pipe.execute.assert_called_with()

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_aquire_lease_redis_leased_expired_lose(self,
                                                    mock_settings,
                                                    mock_time,
                                                    mock_get_primary_cache_source,
                                                    mock_get_connection):
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        mock_time.time.return_value = 999.
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_pipe = mock_get_connection.return_value.pipeline.return_value.__enter__.return_value
        mock_pipe.get.return_value = '99-99-0'
        mock_pipe.execute.side_effect = redis.WatchError
        ret = acquire_lease('a', 1, 1)
        self.assertFalse(ret)
        mock_pipe.watch.assert_called_with('lease-a')
        mock_pipe.get.assert_called_with('lease-a')
        mock_pipe.multi.assert_called_with()
        mock_pipe.expire.assert_called_with('lease-a', 300)
        mock_pipe.set.assert_called_with('lease-a', '1-1-1299')
        mock_pipe.execute.assert_called_with()

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_aquire_lease_redis_leased_expired_wins(self,
                                                    mock_settings,
                                                    mock_time,
                                                    mock_get_primary_cache_source,
                                                    mock_get_connection):
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        mock_time.time.return_value = 999.
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_pipe = mock_get_connection.return_value.pipeline.return_value.__enter__.return_value
        mock_pipe.get.return_value = '99-99-0'
        ret = acquire_lease('a', 1, 1)
        self.assertTrue(ret)
        mock_pipe.watch.assert_called_with('lease-a')
        mock_pipe.get.assert_called_with('lease-a')
        mock_pipe.multi.assert_called_with()
        mock_pipe.expire.assert_called_with('lease-a', 300)
        mock_pipe.set.assert_called_with('lease-a', '1-1-1299')
        mock_pipe.execute.assert_called_with()

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_aquire_lease_redis_leased_owned_lose(self,
                                                  mock_settings,
                                                  mock_time,
                                                  mock_get_primary_cache_source,
                                                  mock_get_connection):
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        mock_time.time.return_value = 999.
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_pipe = mock_get_connection.return_value.pipeline.return_value.__enter__.return_value
        mock_pipe.get.return_value = '99-99-99'
        mock_pipe.execute.side_effect = redis.WatchError
        ret = acquire_lease('a', 99, 99)
        self.assertFalse(ret)
        mock_pipe.watch.assert_called_with('lease-a')
        mock_pipe.get.assert_called_with('lease-a')
        mock_pipe.multi.assert_called_with()
        mock_pipe.expire.assert_called_with('lease-a', 300)
        mock_pipe.set.assert_called_with('lease-a', '99-99-1299')
        mock_pipe.execute.assert_called_with()

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_aquire_lease_redis_leased_owned_wins(self,
                                                  mock_settings,
                                                  mock_time,
                                                  mock_get_primary_cache_source,
                                                  mock_get_connection):
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        mock_time.time.return_value = 999.
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_pipe = mock_get_connection.return_value.pipeline.return_value.__enter__.return_value
        mock_pipe.get.return_value = '99-99-99'
        ret = acquire_lease('a', 99, 99)
        self.assertTrue(ret)
        mock_pipe.watch.assert_called_with('lease-a')
        mock_pipe.get.assert_called_with('lease-a')
        mock_pipe.multi.assert_called_with()
        mock_pipe.expire.assert_called_with('lease-a', 300)
        mock_pipe.set.assert_called_with('lease-a', '99-99-1299')
        mock_pipe.execute.assert_called_with()

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_aquire_lease_redis_leased_fall_through(self,
                                                    mock_settings,
                                                    mock_time,
                                                    mock_get_primary_cache_source,
                                                    mock_get_connection):
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        mock_time.time.return_value = 999.
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_pipe = mock_get_connection.return_value.pipeline.return_value.__enter__.return_value
        mock_pipe.get.return_value = '99-99-999999999'
        ret = acquire_lease('a', 1, 1)
        self.assertFalse(ret)
        mock_pipe.watch.assert_called_with('lease-a')
        mock_pipe.get.assert_called_with('lease-a')
        mock_pipe.multi.assert_called_with()
        self.assertFalse(mock_pipe.expire.called)
        self.assertFalse(mock_pipe.set.called)
        self.assertFalse(mock_pipe.execute.called)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_secondary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_release_lease_redis_failure(self,
                                         mock_settings,
                                         mock_get_secondary_cache_source,
                                         mock_get_connection):
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        mock_get_secondary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_pipe = mock_get_connection.return_value.pipeline.return_value.__enter__.return_value
        mock_pipe.get.return_value = '99-99-99'
        mock_pipe.execute.side_effect = redis.exceptions.ConnectionError
        ret = release_lease('a', 99, 99, 'f', primary=False)
        self.assertTrue(0 is ret)
        mock_pipe.watch.assert_called_with('lease-a')
        mock_pipe.get.assert_called_with('lease-a')
        mock_pipe.multi.assert_called_with()
        mock_pipe.delete.assert_called_with('lease-a')
        mock_pipe.execute.assert_called_with()

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_secondary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_release_lease_redis_not_owned_secondary(self,
                                                     mock_settings,
                                                     mock_get_secondary_cache_source,
                                                     mock_get_connection):
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        mock_get_secondary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_pipe = mock_get_connection.return_value.pipeline.return_value.__enter__.return_value
        mock_pipe.get.return_value = None
        mock_pipe.execute.side_effect = redis.WatchError
        ret = release_lease('a', 1, 1, 'f', primary=False)
        self.assertFalse(ret)
        mock_pipe.watch.assert_called_with('lease-a')
        mock_pipe.get.assert_called_with('lease-a')
        mock_pipe.multi.assert_called_with()
        self.assertFalse(mock_pipe.delete.called)
        self.assertFalse(mock_pipe.execute.called)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_release_lease_redis_not_owned(self,
                                           mock_settings,
                                           mock_get_primary_cache_source,
                                           mock_get_connection):
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_pipe = mock_get_connection.return_value.pipeline.return_value.__enter__.return_value
        mock_pipe.get.return_value = None
        mock_pipe.execute.side_effect = redis.WatchError
        ret = release_lease('a', 1, 1, 'f')
        self.assertFalse(ret)
        mock_pipe.watch.assert_called_with('lease-a')
        mock_pipe.get.assert_called_with('lease-a')
        mock_pipe.multi.assert_called_with()
        self.assertFalse(mock_pipe.delete.called)
        self.assertFalse(mock_pipe.execute.called)

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_release_lease_redis_owned_self_loses(self,
                                                  mock_settings,
                                                  mock_get_primary_cache_source,
                                                  mock_get_connection):
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_pipe = mock_get_connection.return_value.pipeline.return_value.__enter__.return_value
        mock_pipe.get.return_value = '99-99-99'
        mock_pipe.execute.side_effect = redis.WatchError
        ret = release_lease('a', 99, 99, 'f')
        self.assertFalse(ret)
        mock_pipe.watch.assert_called_with('lease-a')
        mock_pipe.get.assert_called_with('lease-a')
        mock_pipe.multi.assert_called_with()
        mock_pipe.delete.assert_called_with('lease-a')
        mock_pipe.execute.assert_called_with()

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_release_lease_redis_owned_self_wins(self,
                                                 mock_settings,
                                                 mock_get_primary_cache_source,
                                                 mock_get_connection):
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_pipe = mock_get_connection.return_value.pipeline.return_value.__enter__.return_value
        mock_pipe.get.return_value = '99-99-99'
        ret = release_lease('a', 99, 99, 'f')
        self.assertTrue(ret)
        mock_pipe.watch.assert_called_with('lease-a')
        mock_pipe.get.assert_called_with('lease-a')
        mock_pipe.multi.assert_called_with()
        mock_pipe.delete.assert_called_with('lease-a')
        mock_pipe.execute.assert_called_with()

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_release_lease_redis_owned_other(self,
                                             mock_settings,
                                             mock_get_primary_cache_source,
                                             mock_get_connection):
        mock_settings.ENDPOINTS = {}
        mock_settings.ELASTICACHE_ENDPOINTS = ELASTICACHE_ENDPOINTS_REDIS
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.ELASTICACHE)
        mock_pipe = mock_get_connection.return_value.pipeline.return_value.__enter__.return_value
        mock_pipe.get.return_value = '99-99-99'
        ret = release_lease('a', 1, 1, 'f')
        self.assertFalse(ret)
        mock_pipe.watch.assert_called_with('lease-a')
        mock_pipe.get.assert_called_with('lease-a')
        mock_pipe.multi.assert_called_with()
        self.assertFalse(mock_pipe.delete.called)
        self.assertFalse(mock_pipe.execute.called)


class LeaseDynamodbTest(unittest.TestCase):

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    def test_aquire_lease_dynamodb_available(self,
                                             mock_time,
                                             mock_get_primary_cache_source,
                                             mock_get_connection):
        mock_time.time.return_value = 999.
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.DYNAMODB)
        mock_get_connection.return_value.update_item.return_value = {'Attributes': {'fence': {'N': '22'}}}
        ret = acquire_lease('a', 1, 1)
        self.assertEqual('22', ret)
        mock_get_connection.return_value.update_item.assert_called_with(
            ReturnValues='ALL_NEW',
            ConditionExpression='attribute_not_exists(lease_state) OR lease_state = :o OR expires < :t '
                                'OR (lease_state = :l AND steps = :s AND retries = :r)',
            TableName='resourcename',
            UpdateExpression='SET fence = if_not_exists(fence, :z) + :f, expires = :e, lease_state = :l, '
                             'steps = :s, retries = :r',
            ExpressionAttributeValues={':l': {'S': 'leased'}, ':o': {'S': 'open'}, ':z': {'N': '0'},
                                       ':t': {'N': '999'}, ':e': {'N': '1299'}, ':f': {'N': '1'},
                                       ':r': {'N': '1'}, ':s': {'N': '1'}}, Key={'ckey': {'S': 'lease-a'}}
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    def test_aquire_lease_dynamodb_unavailable(self,
                                               mock_time,
                                               mock_get_primary_cache_source,
                                               mock_get_connection):
        mock_time.time.return_value = 999.
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.DYNAMODB)
        mock_get_connection.return_value.update_item.side_effect = \
            ClientError({'Error': {'Code': 'ConditionalCheckFailedException'}},
                        'Operation')
        ret = acquire_lease('a', 1, 1)
        self.assertFalse(ret)
        mock_get_connection.return_value.update_item.assert_called_with(
            ReturnValues='ALL_NEW',
            ConditionExpression='attribute_not_exists(lease_state) OR lease_state = :o OR expires < :t '
                                'OR (lease_state = :l AND steps = :s AND retries = :r)',
            TableName='resourcename',
            UpdateExpression='SET fence = if_not_exists(fence, :z) + :f, expires = :e, lease_state = :l, '
                             'steps = :s, retries = :r',
            ExpressionAttributeValues={':l': {'S': 'leased'}, ':o': {'S': 'open'}, ':z': {'N': '0'},
                                       ':t': {'N': '999'}, ':e': {'N': '1299'}, ':f': {'N': '1'},
                                       ':r': {'N': '1'}, ':s': {'N': '1'}}, Key={'ckey': {'S': 'lease-a'}}
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    def test_release_lease_dynamodb_available(self,
                                              mock_time,
                                              mock_get_primary_cache_source,
                                              mock_get_connection):
        mock_time.time.return_value = 999.
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.DYNAMODB)
        mock_get_connection.return_value.update_item.return_value = 'foobar'
        ret = release_lease('a', 1, 1, 'f')
        self.assertTrue(ret)
        mock_get_connection.return_value.update_item.assert_called_with(
            ReturnValues='ALL_NEW',
            ConditionExpression='lease_state = :l AND steps = :s AND retries = :r AND fence = :f',
            TableName='resourcename',
            UpdateExpression='SET lease_state = :o, steps = :null, retries = :null, expires = :null',
            ExpressionAttributeValues={':l': {'S': 'leased'}, ':o': {'S': 'open'}, ':f': {'N': 'f'},
                                       ':null': {'NULL': True}, ':r': {'N': '1'}, ':s': {'N': '1'}},
            Key={'ckey': {'S': 'lease-a'}}
        )

    @mock.patch('aws_lambda_fsm.aws.get_connection')
    @mock.patch('aws_lambda_fsm.aws.get_primary_cache_source')
    @mock.patch('aws_lambda_fsm.aws.time')
    def test_release_lease_dynamodb_unavailable(self,
                                                mock_time,
                                                mock_get_primary_cache_source,
                                                mock_get_connection):
        mock_time.time.return_value = 999.
        mock_get_primary_cache_source.return_value = _get_test_arn(AWS.DYNAMODB)
        mock_get_connection.return_value.update_item.side_effect = \
            ClientError({'Error': {'Code': 'ConditionalCheckFailedException'}},
                        'Operation')
        ret = release_lease('a', 1, 1, 'f')
        self.assertFalse(ret)
        mock_get_connection.return_value.update_item.assert_called_with(
            ReturnValues='ALL_NEW',
            ConditionExpression='lease_state = :l AND steps = :s AND retries = :r AND fence = :f',
            TableName='resourcename',
            UpdateExpression='SET lease_state = :o, steps = :null, retries = :null, expires = :null',
            ExpressionAttributeValues={':l': {'S': 'leased'}, ':o': {'S': 'open'}, ':f': {'N': 'f'},
                                       ':null': {'NULL': True}, ':r': {'N': '1'}, ':s': {'N': '1'}},
            Key={'ckey': {'S': 'lease-a'}}
        )


class ValidateConfigTest(unittest.TestCase):

    # _validate_sqs_urls

    @mock.patch('aws_lambda_fsm.aws.logger')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_validate_sqs_urls_empty_valid(self,
                                           mock_settings,
                                           mock_logger):
        mock_settings.SQS_URLS = {}
        _validate_sqs_urls()
        self.assertEqual(
            [],
            mock_logger.mock_calls
        )

    @mock.patch('aws_lambda_fsm.aws.logger')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_validate_sqs_urls_wrong_service(self,
                                             mock_settings,
                                             mock_logger):
        mock_settings.SQS_URLS = {
            _get_test_arn(AWS.DYNAMODB): {
                AWS_SQS.QueueUrl: 'http://host/queue'
            }
        }
        _validate_sqs_urls()
        self.assertEqual(
            [mock.call.warning("SQS_URLS has invalid key '%s' (service)",
                               'arn:aws:dynamodb:testing:1234567890:resourcetype/resourcename')],
            mock_logger.mock_calls
        )

    @mock.patch('aws_lambda_fsm.aws.logger')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_validate_sqs_urls_missing_queue(self,
                                             mock_settings,
                                             mock_logger):
        mock_settings.SQS_URLS = {
            _get_test_arn(AWS.SQS): {
                'foo': 'http://host/queue'
            }
        }
        _validate_sqs_urls()
        self.assertEqual(
            [mock.call.warning("SQS_URLS has invalid entry for key '%s' (url)",
                               'arn:aws:sqs:testing:1234567890:resourcetype/resourcename')],
            mock_logger.mock_calls
        )

    @mock.patch('aws_lambda_fsm.aws.logger')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_validate_sqs_urls(self,
                               mock_settings,
                               mock_logger):
        mock_settings.SQS_URLS = {
            _get_test_arn(AWS.SQS): {
                AWS_SQS.QueueUrl: 'http://host/queue'
            }
        }
        _validate_sqs_urls()
        self.assertEqual(
            [],
            mock_logger.mock_calls
        )

    # _validate_elasticache_endpoints

    @mock.patch('aws_lambda_fsm.aws.logger')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_validate_validate_elasticache_endpoints_empty_valid(self,
                                                                 mock_settings,
                                                                 mock_logger):
        mock_settings.ELASTICACHE_ENDPOINTS = {}
        _validate_elasticache_endpoints()
        self.assertEqual(
            [],
            mock_logger.mock_calls
        )

    @mock.patch('aws_lambda_fsm.aws.logger')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_validate_validate_elasticache_endpoints_wrong_service(self,
                                                                   mock_settings,
                                                                   mock_logger):
        mock_settings.ELASTICACHE_ENDPOINTS = {
            _get_test_arn(AWS.DYNAMODB): {
                AWS_ELASTICACHE.Engine: AWS_ELASTICACHE.ENGINE.MEMCACHED,
                AWS_ELASTICACHE.ConfigurationEndpoint: {
                    AWS_ELASTICACHE.CONFIGURATION_ENDPOINT.Address: "host",
                    AWS_ELASTICACHE.CONFIGURATION_ENDPOINT.Port: 1111,
                }
            }
        }
        _validate_elasticache_endpoints()
        self.assertEqual(
            [mock.call.warning("ELASTICACHE_ENDPOINTS has invalid key '%s'",
                               'arn:aws:dynamodb:testing:1234567890:resourcetype/resourcename')],
            mock_logger.mock_calls
        )

    @mock.patch('aws_lambda_fsm.aws.logger')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_validate_validate_elasticache_endpoints_missing_engine(self,
                                                                    mock_settings,
                                                                    mock_logger):
        mock_settings.ELASTICACHE_ENDPOINTS = {
            _get_test_arn(AWS.ELASTICACHE): {
                'foo': AWS_ELASTICACHE.ENGINE.MEMCACHED,
                AWS_ELASTICACHE.ConfigurationEndpoint: {
                    AWS_ELASTICACHE.CONFIGURATION_ENDPOINT.Address: "host",
                    AWS_ELASTICACHE.CONFIGURATION_ENDPOINT.Port: 1111,
                }
            }
        }
        _validate_elasticache_endpoints()
        self.assertEqual(
            [mock.call.warning("ELASTICACHE_ENDPOINTS has invalid entry for key '%s' (engine)",
                               'arn:aws:elasticache:testing:1234567890:resourcetype/resourcename')],
            mock_logger.mock_calls
        )

    @mock.patch('aws_lambda_fsm.aws.logger')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_validate_validate_elasticache_endpoints_unknown_engine(self,
                                                                    mock_settings,
                                                                    mock_logger):
        mock_settings.ELASTICACHE_ENDPOINTS = {
            _get_test_arn(AWS.ELASTICACHE): {
                AWS_ELASTICACHE.Engine: 'unknown',
                AWS_ELASTICACHE.ConfigurationEndpoint: {
                    AWS_ELASTICACHE.CONFIGURATION_ENDPOINT.Address: "host",
                    AWS_ELASTICACHE.CONFIGURATION_ENDPOINT.Port: 1111,
                }
            }
        }
        _validate_elasticache_endpoints()
        self.assertEqual(
            [mock.call.warning("ELASTICACHE_ENDPOINTS has invalid entry for key '%s' (unknown engine)",
                               'arn:aws:elasticache:testing:1234567890:resourcetype/resourcename')],
            mock_logger.mock_calls
        )

    @mock.patch('aws_lambda_fsm.aws.logger')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_validate_validate_elasticache_endpoints_missing_endpoint(self,
                                                                      mock_settings,
                                                                      mock_logger):
        mock_settings.ELASTICACHE_ENDPOINTS = {
            _get_test_arn(AWS.ELASTICACHE): {
                AWS_ELASTICACHE.Engine: AWS_ELASTICACHE.ENGINE.MEMCACHED,
                'foo': {
                    AWS_ELASTICACHE.CONFIGURATION_ENDPOINT.Address: "host",
                    AWS_ELASTICACHE.CONFIGURATION_ENDPOINT.Port: 1111,
                }
            }
        }
        _validate_elasticache_endpoints()
        self.assertEqual(
            [mock.call.warning("ELASTICACHE_ENDPOINTS has invalid entry for key '%s' (endpoint)",
                               'arn:aws:elasticache:testing:1234567890:resourcetype/resourcename')],
            mock_logger.mock_calls
        )

    @mock.patch('aws_lambda_fsm.aws.logger')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_validate_validate_elasticache_endpoints_missing_address_port(self,
                                                                          mock_settings,
                                                                          mock_logger):
        mock_settings.ELASTICACHE_ENDPOINTS = {
            _get_test_arn(AWS.ELASTICACHE): {
                AWS_ELASTICACHE.Engine: AWS_ELASTICACHE.ENGINE.MEMCACHED,
                AWS_ELASTICACHE.ConfigurationEndpoint: {
                    'foo': "host",
                    'bar': 1111,
                }
            }
        }
        _validate_elasticache_endpoints()
        self.assertEqual(
            [mock.call.warning("ELASTICACHE_ENDPOINTS has invalid entry for key '%s' (address)",
                               'arn:aws:elasticache:testing:1234567890:resourcetype/resourcename'),
             mock.call.warning("ELASTICACHE_ENDPOINTS has invalid entry for key '%s' (port)",
                               'arn:aws:elasticache:testing:1234567890:resourcetype/resourcename')],
            mock_logger.mock_calls
        )

    @mock.patch('aws_lambda_fsm.aws.logger')
    @mock.patch('aws_lambda_fsm.aws.settings')
    def test_validate_validate_elasticache_endpoints(self,
                                                     mock_settings,
                                                     mock_logger):
        mock_settings.ELASTICACHE_ENDPOINTS = {
            _get_test_arn(AWS.ELASTICACHE): {
                AWS_ELASTICACHE.Engine: AWS_ELASTICACHE.ENGINE.MEMCACHED,
                AWS_ELASTICACHE.ConfigurationEndpoint: {
                    AWS_ELASTICACHE.CONFIGURATION_ENDPOINT.Address: "host",
                    AWS_ELASTICACHE.CONFIGURATION_ENDPOINT.Port: 1111,
                }
            }
        }
        _validate_elasticache_endpoints()
        self.assertEqual(
            [],
            mock_logger.mock_calls
        )

    # validate_config

    @mock.patch('aws_lambda_fsm.aws._validate_config')
    def test_validate_config_runs_once(self, mock_validate_config):
        _local.validated_config = False
        self.assertEqual(0, len(mock_validate_config.mock_calls))
        validate_config()
        self.assertEqual(6, len(mock_validate_config.mock_calls))
        _local.validated_config = True
        validate_config()
        self.assertEqual(6, len(mock_validate_config.mock_calls))

    @mock.patch('aws_lambda_fsm.aws._validate_config')
    def test_validate_config(self, mock_validate_config):
        _local.validated_config = False
        validate_config()
        mock_validate_config.assert_called_with(
            'STREAM',
            {'failover': True,
             'required': True,
             'primary': 'arn:partition:kinesis:testing:account:stream/resource',
             'secondary': None,
             'allowed': ['kinesis', 'dynamodb', 'sns', 'sqs']}
        )

    @mock.patch('aws_lambda_fsm.aws.logger')
    def test_required_failover_unset_sources(self, mock_logger):
        _validate_config('KEY',
                         {'required': True,
                          'failover': True,
                          'primary': None,
                          'secondary': None,
                          'allowed': ['foo']})
        self.assertEqual(
            [
                mock.call.fatal('PRIMARY_%s_SOURCE is unset.',
                                'KEY'),
                mock.call.warning('SECONDARY_%s_SOURCE is unset (failover not configured).',
                                  'KEY')
            ],
            mock_logger.mock_calls
        )

    @mock.patch('aws_lambda_fsm.aws.logger')
    def test_required_failover(self, mock_logger):
        _validate_config('KEY',
                         {'required': True,
                          'failover': True,
                          'primary': 'arn:partition:bar:testing:account:stream/resource',
                          'secondary': 'arn:partition:bar:testing:account:stream/resource',
                          'allowed': ['foo']})
        self.assertEqual(
            [
                mock.call.fatal("PRIMARY_%s_SOURCE '%s' is not allowed.",
                                'KEY', 'arn:partition:bar:testing:account:stream/resource'),
                mock.call.fatal("SECONDARY_%s_SOURCE '%s' is not allowed.",
                                'KEY', 'arn:partition:bar:testing:account:stream/resource'),
                mock.call.warning('PRIMARY_%s_SOURCE = SECONDARY_%s_SOURCE (failover not configured optimally).',
                                  'KEY', 'KEY')
            ],
            mock_logger.mock_calls
        )
