# -*- coding: utf-8 -*-

"""Tests for go.apps.opt_out application"""

from twisted.internet.defer import inlineCallbacks, returnValue

from vumi.message import TransportUserMessage

from go.apps.opt_out.vumi_app import OptOutApplication
from go.vumitools.tests.utils import AppWorkerTestCase
from go.vumitools.opt_out import OptOutStore


class TestOptOutApplication(AppWorkerTestCase):

    application_class = OptOutApplication
    transport_type = u'sms'

    @inlineCallbacks
    def setUp(self):
        super(TestOptOutApplication, self).setUp()

        # Setup the OptOutApplication
        self.app = yield self.get_application({})

        # Steal app's vumi_api
        self.vumi_api = self.app.vumi_api  # YOINK!
        self._persist_riak_managers.append(self.vumi_api.manager)

        # Create a test user account
        self.user_account = yield self.mk_user(self.vumi_api, u'testuser')
        self.user_api = self.vumi_api.get_user_api(self.user_account.key)

        # Add tags
        yield self.setup_tagpools()

        self.conversation = yield self.create_conversation()

    @inlineCallbacks
    def opt_out(self, from_addr, to_addr, content, transport_type=None,
                helper_metadata=None):
        if transport_type:
            self.transport_type = transport_type
        if helper_metadata is None:
            helper_metadata = {"go": {"user_account": "testuser"}}
        msg = TransportUserMessage(
            to_addr=to_addr,
            from_addr=from_addr,
            content=content,
            session_event=None,
            transport_name=self.transport_name,
            transport_type=self.transport_type,
            helper_metadata=helper_metadata,
            )
        yield self.dispatch(msg)

    @inlineCallbacks
    def wait_for_messages(self, nr_of_messages, total_length):
        msgs = yield self.wait_for_dispatched_messages(total_length)
        returnValue(msgs[-1 * nr_of_messages:])

    @inlineCallbacks
    def test_sms_opt_out(self):
        yield self.start_conversation(self.conversation)
        yield self.opt_out("12345", "666", "STOP")
        [msg] = self.get_dispatched_messages()
        self.assertEqual(msg.get('content'), "You have opted out")
        opt_out_store = OptOutStore(self.vumi_api.manager, "testuser")
        opt_out = yield opt_out_store.get_opt_out("msisdn", "12345")
        self.assertNotEqual(opt_out, None)

    @inlineCallbacks
    def test_sms_opt_out_no_account(self):
        yield self.start_conversation(self.conversation)
        yield self.opt_out("12345", "666", "STOP", helper_metadata={})
        [msg] = self.get_dispatched_messages()
        self.assertEqual(msg.get('content'),
                         "Your opt-out was received but we failed to link it "
                         "to a specific service, please try again later.")

    @inlineCallbacks
    def test_http_opt_out(self):
        yield self.start_conversation(self.conversation)
        yield self.opt_out("12345", "666", "STOP", "http_api")
        [msg] = yield self.wait_for_dispatched_messages(1)
        self.assertEqual(msg.get('content'),
                '{"msisdn":"12345","opted_in": false}')
        opt_out_store = OptOutStore(self.vumi_api.manager, "testuser")
        opt_out = yield opt_out_store.get_opt_out("msisdn", "12345")
        self.assertNotEqual(opt_out, None)
