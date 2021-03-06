# -*- coding: utf-8 -*-

"""Tests for go.vumitools.api."""

from twisted.trial.unittest import TestCase
from twisted.internet.defer import inlineCallbacks, returnValue

from vumi.tests.utils import get_fake_amq_client
from vumi.errors import VumiError

from go.vumitools.opt_out import OptOutStore
from go.vumitools.contact import ContactStore
from go.vumitools.api import (
    VumiApi, VumiUserApi, VumiApiCommand, VumiApiEvent)
from go.vumitools.tests.utils import AppWorkerTestCase, FakeAmqpConnection
from go.vumitools.account.old_models import AccountStoreVNone, AccountStoreV1
from go.vumitools.account.models import GoConnector, RoutingTableHelper


class TestTxVumiApi(AppWorkerTestCase):
    @inlineCallbacks
    def setUp(self):
        yield super(TestTxVumiApi, self).setUp()
        if self.sync_persistence:
            # Set up the vumi exchange, in case we don't have one.
            self._amqp.exchange_declare('vumi', 'direct')
            self.vumi_api = VumiApi.from_config_sync(
                self._persist_config, FakeAmqpConnection(self._amqp))
        else:
            self.vumi_api = yield VumiApi.from_config_async(
                self._persist_config, get_fake_amq_client(self._amqp))
        self._persist_riak_managers.append(self.vumi_api.manager)
        self._persist_redis_managers.append(self.vumi_api.redis)

    @inlineCallbacks
    def test_declare_tags_from_different_pools(self):
        tag1, tag2 = ("poolA", "tag1"), ("poolB", "tag2")
        yield self.vumi_api.tpm.declare_tags([tag1, tag2])
        self.assertEqual((yield self.vumi_api.tpm.acquire_tag("poolA")), tag1)
        self.assertEqual((yield self.vumi_api.tpm.acquire_tag("poolB")), tag2)

    @inlineCallbacks
    def test_send_command(self):
        for addr in ["+12", "+34"]:
            yield self.vumi_api.send_command(
                    "dummy_worker", "send",
                    batch_id="b123", content="Hello!",
                    msg_options={'from_addr': '+56'}, to_addr=addr)

        [cmd1, cmd2] = self.get_dispatcher_commands()
        self.assertEqual(cmd1.payload['kwargs']['to_addr'], '+12')
        self.assertEqual(cmd2.payload['kwargs']['to_addr'], '+34')


class TestVumiApi(TestTxVumiApi):
    sync_persistence = True


class TestTxVumiUserApi(AppWorkerTestCase):
    @inlineCallbacks
    def setUp(self):
        yield super(TestTxVumiUserApi, self).setUp()
        if self.sync_persistence:
            self.vumi_api = VumiApi.from_config_sync(self._persist_config)
        else:
            self.vumi_api = yield VumiApi.from_config_async(
                self._persist_config)
        self.user_account = yield self.mk_user(self.vumi_api, u'Buster')
        self.user_api = VumiUserApi(self.vumi_api, self.user_account.key)

        # Some stores for old versions to test migrations.
        self.account_store_vnone = AccountStoreVNone(self.vumi_api.manager)
        self.account_store_v1 = AccountStoreV1(self.vumi_api.manager)

    @inlineCallbacks
    def test_optout_filtering(self):
        group = yield self.user_api.contact_store.new_group(u'test-group')
        optout_store = OptOutStore.from_user_account(self.user_account)
        contact_store = ContactStore.from_user_account(self.user_account)

        # Create two random contacts
        yield self.user_api.contact_store.new_contact(
            msisdn=u'+27761234567', groups=[group.key])
        yield self.user_api.contact_store.new_contact(
            msisdn=u'+27760000000', groups=[group.key])

        conv = yield self.create_conversation(
            conversation_type=u'dummy', delivery_class=u'sms')
        conv.add_group(group)
        yield conv.save()

        # Opt out the first contact
        yield optout_store.new_opt_out(u'msisdn', u'+27761234567', {
            'message_id': u'the-message-id'
        })
        contact_keys = yield contact_store.get_contacts_for_conversation(conv)
        all_addrs = []
        for contacts in contact_store.contacts.load_all_bunches(contact_keys):
            for contact in (yield contacts):
                all_addrs.append(contact.addr_for(conv.delivery_class))
        self.assertEqual(set(all_addrs), set(['+27760000000', '+27761234567']))
        optedin_addrs = []
        for contacts in (yield conv.get_opted_in_contact_bunches(
                conv.delivery_class)):
            for contact in (yield contacts):
                optedin_addrs.append(contact.addr_for(conv.delivery_class))
        self.assertEqual(optedin_addrs, ['+27760000000'])

    @inlineCallbacks
    def test_exists(self):
        self.assertTrue(
            (yield self.vumi_api.user_exists(self.user_account.key)))
        self.assertTrue((yield self.user_api.exists()))

        self.assertFalse((yield self.vumi_api.user_exists('foo')))
        self.assertFalse((yield VumiUserApi(self.vumi_api, 'foo').exists()))

    @inlineCallbacks
    def test_list_endpoints(self):
        tag1, tag2, tag3 = yield self.setup_tagpool(
            u"pool1", [u"1234", u"5678", u"9012"])
        yield self.user_api.acquire_specific_tag(tag1)
        endpoints = yield self.user_api.list_endpoints()
        self.assertEqual(endpoints, set([tag1]))

    @inlineCallbacks
    def assert_account_tags(self, expected):
        user_account = yield self.user_api.get_user_account()
        self.assertEqual(expected, user_account.tags)

    @inlineCallbacks
    def test_declare_acquire_and_release_tags(self):
        tag1, tag2 = ("poolA", "tag1"), ("poolA", "tag2")
        yield self.vumi_api.tpm.declare_tags([tag1, tag2])
        yield self.add_tagpool_permission(u"poolA")
        yield self.add_tagpool_permission(u"poolB")

        yield self.assert_account_tags([])
        tag2_info = yield self.vumi_api.mdb.get_tag_info(tag2)
        self.assertEqual(tag2_info.metadata['user_account'], None)
        self.assertEqual(tag2_info.current_batch.key, None)
        self.assertEqual((yield self.user_api.acquire_tag(u"poolA")), tag1)
        self.assertEqual((yield self.user_api.acquire_tag(u"poolA")), tag2)
        self.assertEqual((yield self.user_api.acquire_tag(u"poolA")), None)
        self.assertEqual((yield self.user_api.acquire_tag(u"poolB")), None)
        yield self.assert_account_tags([list(tag1), list(tag2)])
        tag2_info = yield self.vumi_api.mdb.get_tag_info(tag2)
        self.assertEqual(tag2_info.metadata['user_account'],
                         self.user_api.user_account_key)
        self.assertNotEqual(tag2_info.current_batch.key, None)

        yield self.user_api.release_tag(tag2)
        yield self.assert_account_tags([list(tag1)])
        tag2_info = yield self.vumi_api.mdb.get_tag_info(tag2)
        self.assertEqual(tag2_info.metadata['user_account'], None)
        self.assertEqual(tag2_info.current_batch.key, None)
        self.assertEqual((yield self.user_api.acquire_tag(u"poolA")), tag2)
        self.assertEqual((yield self.user_api.acquire_tag(u"poolA")), None)
        yield self.assert_account_tags([list(tag1), list(tag2)])

    @inlineCallbacks
    def test_batch_id_for_specific_tag(self):
        [tag] = yield self.setup_tagpool(u"poolA", [u"tag1"])
        yield self.user_api.acquire_specific_tag(tag)
        tag_info = yield self.vumi_api.mdb.get_tag_info(tag)
        self.assertNotEqual(tag_info.current_batch.key, None)

    def _set_routing_table(self, user, entries):
        # Each entry is a tuple of (src, dst) where src and dst are
        # conversations, tags or connector strings.
        user.routing_table = {}
        rt_helper = RoutingTableHelper(user.routing_table)

        def mkconn(thing):
            if isinstance(thing, basestring):
                # Use it as-is.
                return thing
            elif isinstance(thing, tuple):
                # It's a tag.
                return str(GoConnector.for_transport_tag(thing[0], thing[1]))
            else:
                # Assume it's a conversation.
                return str(GoConnector.for_conversation(
                    thing.conversation_type, thing.key))

        for src, dst in entries:
            rt_helper.add_entry(mkconn(src), "default", mkconn(dst), "default")

    @inlineCallbacks
    def test_release_tag_with_routing_entries(self):
        [tag1] = yield self.setup_tagpool(u"pool1", [u"1234"])
        yield self.assert_account_tags([])
        yield self.user_api.acquire_specific_tag(tag1)
        yield self.assert_account_tags([list(tag1)])

        conv = yield self.user_api.new_conversation(
            u'bulk_message', u'name', u'desc', {})
        user = yield self.user_api.get_user_account()
        self._set_routing_table(user, [(conv, tag1), (tag1, conv)])
        yield user.save()

        self.assertNotEqual({}, (yield self.user_api.get_routing_table()))
        yield self.user_api.release_tag(tag1)
        yield self.assert_account_tags([])
        self.assertEqual({}, (yield self.user_api.get_routing_table()))

    @inlineCallbacks
    def test_get_empty_routing_table(self):
        routing_table = yield self.user_api.get_routing_table()
        self.assertEqual({}, routing_table)

    @inlineCallbacks
    def _setup_routing_table_test_new_conv(self, routing_table=None):
        tag1, tag2, tag3 = yield self.setup_tagpool(
            u"pool1", [u"1234", u"5678", u"9012"])
        yield self.user_api.acquire_specific_tag(tag1)
        conv = yield self.user_api.new_conversation(
            u'bulk_message', u'name', u'desc', {})
        conv = self.user_api.wrap_conversation(conv)
        # We don't want to actually send commands here.
        conv.dispatch_command = lambda *args, **kw: None
        yield conv.start()

        # Set the status manually, because it's in `starting', not `running'
        conv.set_status_started()
        yield conv.save()

        returnValue(conv)

    @inlineCallbacks
    def test_get_routing_table(self):
        conv = yield self._setup_routing_table_test_new_conv()
        tag = (u'pool1', u'1234')
        user = yield self.user_api.get_user_account()
        self._set_routing_table(user, [(conv, tag), (tag, conv)])
        yield user.save()
        routing_table = yield self.user_api.get_routing_table()
        self.assertEqual(routing_table, {
            u':'.join([u'CONVERSATION:bulk_message', conv.key]): {
                u'default': [u'TRANSPORT_TAG:pool1:1234', u'default']},
            u'TRANSPORT_TAG:pool1:1234': {
                u'default': [
                    u'CONVERSATION:bulk_message:%s' % conv.key, u'default']},
        })

        # TODO: This belongs in a different test.
        yield conv.archive_conversation()

        routing_table = yield self.user_api.get_routing_table()
        self.assertEqual(routing_table, {})

    @inlineCallbacks
    def test_routing_table_validation_valid(self):
        conv = yield self._setup_routing_table_test_new_conv()
        tag = (u'pool1', u'1234')
        user = yield self.user_api.get_user_account()
        self._set_routing_table(user, [(conv, tag), (tag, conv)])
        yield user.save()
        yield self.user_api.validate_routing_table(user)

    @inlineCallbacks
    def test_routing_table_invalid_src_conn_tag(self):
        conv = yield self._setup_routing_table_test_new_conv()
        tag = (u'pool1', u'1234')
        badtag = (u'badpool', u'bad')
        user = yield self.user_api.get_user_account()
        self._set_routing_table(user, [(conv, tag), (badtag, conv)])
        yield user.save()
        try:
            yield self.user_api.validate_routing_table(user)
            self.fail("Expected VumiError, got no exception.")
        except VumiError as e:
            self.assertTrue('badpool:bad' in str(e))

    @inlineCallbacks
    def test_routing_table_invalid_dst_conn_tag(self):
        conv = yield self._setup_routing_table_test_new_conv()
        tag = (u'pool1', u'1234')
        badtag = (u'badpool', u'bad')
        user = yield self.user_api.get_user_account()
        self._set_routing_table(user, [(conv, badtag), (tag, conv)])
        yield user.save()
        try:
            yield self.user_api.validate_routing_table(user)
            self.fail("Expected VumiError, got no exception.")
        except VumiError as e:
            self.assertTrue('TRANSPORT_TAG:badpool:bad' in str(e))

    @inlineCallbacks
    def test_routing_table_invalid_src_conn_conv(self):
        conv = yield self._setup_routing_table_test_new_conv()
        tag = (u'pool1', u'1234')
        badconv = 'CONVERSATION:bulk_message:badkey'
        user = yield self.user_api.get_user_account()
        self._set_routing_table(user, [(badconv, tag), (tag, conv)])
        yield user.save()
        try:
            yield self.user_api.validate_routing_table(user)
            self.fail("Expected VumiError, got no exception.")
        except VumiError as e:
            self.assertTrue('CONVERSATION:bulk_message:badkey' in str(e))

    @inlineCallbacks
    def test_routing_table_invalid_dst_conn_conv(self):
        conv = yield self._setup_routing_table_test_new_conv()
        tag = (u'pool1', u'1234')
        badconv = 'CONVERSATION:bulk_message:badkey'
        user = yield self.user_api.get_user_account()
        self._set_routing_table(user, [(conv, tag), (tag, badconv)])
        yield user.save()
        try:
            yield self.user_api.validate_routing_table(user)
            self.fail("Expected VumiError, got no exception.")
        except VumiError as e:
            self.assertTrue('CONVERSATION:bulk_message:badkey' in str(e))


class TestVumiUserApi(TestTxVumiUserApi):
    sync_persistence = True


class TestTxVumiRouterApi(AppWorkerTestCase):
    @inlineCallbacks
    def setUp(self):
        yield super(TestTxVumiRouterApi, self).setUp()
        if self.sync_persistence:
            # Set up the vumi exchange, in case we don't have one.
            self._amqp.exchange_declare('vumi', 'direct')
            self.vumi_api = VumiApi.from_config_sync(
                self._persist_config, FakeAmqpConnection(self._amqp))
        else:
            self.vumi_api = yield VumiApi.from_config_async(
                self._persist_config, get_fake_amq_client(self._amqp))
        self._persist_riak_managers.append(self.vumi_api.manager)
        self._persist_redis_managers.append(self.vumi_api.redis)
        self.user_account = yield self.mk_user(self.vumi_api, u'Buster')
        self.user_api = VumiUserApi(self.vumi_api, self.user_account.key)

    def create_router(self, **kw):
        # TODO: Fix test infrastructe to avoid duplicating this stuff.
        router_type = kw.pop('router_type', u'keyword')
        name = kw.pop('name', u'routername')
        description = kw.pop('description', u'')
        config = kw.pop('config', {})
        self.assertTrue(isinstance(config, dict))
        return self.user_api.new_router(
            router_type, name, description, config, **kw)

    @inlineCallbacks
    def get_router_api(self, router=None):
        if router is None:
            router = yield self.create_router()
        returnValue(
            self.user_api.get_router_api(router.router_type, router.key))

    def test_get_router(self):
        router = yield self.create_router()
        router_api = yield self.get_router_api(router)
        got_router = yield router_api.get_router()
        self.assertEqual(router.router_type, got_router.router_type)
        self.assertEqual(router.key, got_router.key)
        self.assertEqual(router.name, got_router.name)
        self.assertEqual(router.description, got_router.description)
        self.assertEqual(router.config, got_router.config)

    @inlineCallbacks
    def _add_routing_entries(self, rapi):
        conv_conn = 'CONVERSATION:type:key'
        tag_conn = 'TRANSPORT_TAG:pool:tag'
        rin_conn = str(GoConnector.for_router(
            rapi.router_type, rapi.router_key, GoConnector.INBOUND))
        rout_conn = str(GoConnector.for_router(
            rapi.router_type, rapi.router_key, GoConnector.OUTBOUND))

        user_account = yield self.user_api.get_user_account()
        rt_helper = RoutingTableHelper(user_account.routing_table)
        rt_helper.add_entry(tag_conn, 'default', rin_conn, 'default')
        rt_helper.add_entry(rin_conn, 'default', tag_conn, 'default')
        rt_helper.add_entry(conv_conn, 'default', rout_conn, 'default')
        rt_helper.add_entry(rout_conn, 'default', conv_conn, 'default')
        yield user_account.save()

    @inlineCallbacks
    def test_archive_router(self):
        router = yield self.create_router()
        router_api = yield self.get_router_api(router)
        yield self._add_routing_entries(router_api)
        self.assertEqual(router.archive_status, 'active')
        self.assertNotEqual({}, (yield self.user_api.get_routing_table()))

        yield router_api.archive_router()
        router = yield router_api.get_router()
        self.assertEqual(router.archive_status, 'archived')
        self.assertEqual({}, (yield self.user_api.get_routing_table()))

    @inlineCallbacks
    def test_start_router(self):
        router = yield self.create_router()
        router_api = yield self.get_router_api(router)
        self.assertTrue(router.stopped())
        self.assertFalse(router.starting())
        self.assertEqual([], self.get_dispatcher_commands())

        yield router_api.start_router()
        router = yield router_api.get_router()
        self.assertFalse(router.stopped())
        self.assertTrue(router.starting())
        [cmd] = self.get_dispatcher_commands()
        self.assertEqual(cmd['command'], 'start')
        self.assertEqual(cmd['kwargs'], {
            'user_account_key': router.user_account.key,
            'router_key': router.key,
        })

    @inlineCallbacks
    def test_stop_router(self):
        router = yield self.create_router(status=u'running')
        router_api = yield self.get_router_api(router)
        self.assertTrue(router.running())
        self.assertFalse(router.stopping())
        self.assertEqual([], self.get_dispatcher_commands())

        yield router_api.stop_router()
        router = yield router_api.get_router()
        self.assertFalse(router.running())
        self.assertTrue(router.stopping())
        [cmd] = self.get_dispatcher_commands()
        self.assertEqual(cmd['command'], 'stop')
        self.assertEqual(cmd['kwargs'], {
            'user_account_key': router.user_account.key,
            'router_key': router.key,
        })


class TestVumiRouterApi(TestTxVumiRouterApi):
    sync_persistence = True


class TestVumiApiCommand(TestCase):
    def test_default_routing_config(self):
        cfg = VumiApiCommand.default_routing_config()
        self.assertEqual(cfg, {
            'exchange': 'vumi',
            'exchange_type': 'direct',
            'routing_key': 'vumi.api',
            'durable': True,
            })


class TestVumiApiEvent(TestCase):
    def test_default_routing_config(self):
        cfg = VumiApiEvent.default_routing_config()
        self.assertEqual(cfg, {
            'exchange': 'vumi',
            'exchange_type': 'direct',
            'routing_key': 'vumi.event',
            'durable': True,
            })

    def test_event(self):
        event = VumiApiEvent.event(
            'me', 'my_conv', 'my_event', {"foo": "bar"})
        self.assertEqual(event['account_key'], 'me')
        self.assertEqual(event['conversation_key'], 'my_conv')
        self.assertEqual(event['event_type'], 'my_event')
        self.assertEqual(event['content'], {"foo": "bar"})
